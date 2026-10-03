// Package hooks 实现三个 Codex 命令行钩子入口：session-start / prompt-submit / stop。
//
// 逐条对齐 Python 版 plugins\voice-pill\hook_session_start.py、
// hook_prompt_submit.py、hook_stop.py。两条铁律（都是被踩过之后定的）：
//
//  1. 任何情况下往 stdout 恰好打一行 JSON——不打，Codex 会等，于是「钩子卡住」
//     直接变成「对话卡住」。stdout 直接写字节，UTF-8 天然成立（Python 侧要
//     console.make_output_safe()，Go 侧不需要）。
//  2. 永远返回 0：引擎没开、管道断了、正文超长，都不许拦住用户的这一轮。
package hooks

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"voicepill/internal/bridge"
	"voicepill/internal/engine"
)

// 三个子命令名，与 main.go 的分发、hooks.json 里的入口一一对应。
const (
	KindSessionStart = "session-start"
	KindPromptSubmit = "prompt-submit"
	KindStop         = "stop"
)

// hookCallTimeout 对齐 Python 版钩子里的 timeout=5.0。
//
// 为什么不直接用 bridge.Client.Call 的命令表超时：那张表（take 8 秒、
// speak 10 秒）是给 MCP 工具留的余量，而 hooks.json 给 Stop 的预算只有 5 秒。
// 超过预算 Codex 会把钩子判失败，所以钩子这一层自己卡 5 秒。
const hookCallTimeout = 5 * time.Second

// maxSpeakChars 对齐 Python hook_stop.py 的 MAX_CHARS。按字符截，不是按字节。
const maxSpeakChars = 4000

// promptPrefix 与 Python hook_prompt_submit.py 的正文逐字一致
// （与 internal/tools 里 voice_pill_prompt_hook 的同一句话也一致）。
const promptPrefix = "用户刚刚用语音输入（Voice Pill）说了以下内容，请把它当作本轮意图的一部分：\n"

// Caller 是控制面客户端在钩子层看到的形状。生产实现是 bridge.Client，
// 单测里换成假实现。
type Caller interface {
	Call(cmd string, args map[string]any) (json.RawMessage, error)
}

// Run 是 main.go 的入口。永远返回 0。
func Run(kind string, in io.Reader, out io.Writer) int {
	return run(kind, in, out, bridge.NewClient())
}

// run 是 Run 的可注入版本（单测用假 Caller）。
func run(kind string, in io.Reader, out io.Writer, caller Caller) int {
	switch kind {
	case KindPromptSubmit:
		promptSubmit(in, out, caller)
	case KindStop:
		stop(in, out, caller)
	case KindSessionStart:
		sessionStart(in, out, caller)
	default:
		writeJSONLine(out, continueOutput{Continue: true})
	}
	return 0
}

// continueOutput 是「放行」输出：没有别的话要说时三个钩子都打它。
type continueOutput struct {
	Continue bool `json:"continue"`
}

// promptOutput 是 UserPromptSubmit 的注入输出，字段名与 Python 版一致。
type promptOutput struct {
	HookSpecificOutput promptSpecificOutput `json:"hookSpecificOutput"`
}

type promptSpecificOutput struct {
	HookEventName     string `json:"hookEventName"`
	AdditionalContext string `json:"additionalContext"`
}

// writeJSONLine 往 out 写恰好一行 JSON（Encode 自带换行）。不转义 HTML——
// Python 的 json.dumps 也不转，别让 < > & 在模型看到之前变成 \u003c。
func writeJSONLine(out io.Writer, v any) {
	if out == nil {
		return
	}
	enc := json.NewEncoder(out)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

// readPayload 读钩子的 stdin。读不到 / 空 / 不是 JSON / 不是对象 → 空对象，
// 与 Python 的 _read_stdin 一致：钩子的入参不该成为失败原因。
func readPayload(in io.Reader) map[string]any {
	if in == nil {
		return map[string]any{}
	}
	raw, err := io.ReadAll(in)
	if err != nil || len(strings.TrimSpace(string(raw))) == 0 {
		return map[string]any{}
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil || data == nil {
		return map[string]any{}
	}
	return data
}

// textOf 复刻 Python 的 str(value or "")：缺失当空串，别的走 %v。
func textOf(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

// truncateRunes 按字符截断——Python 的 text[:4000] 也是按字符，Go 若按字节
// 会把一个中文字砍成两半。
func truncateRunes(s string, limit int) string {
	runes := []rune(s)
	if len(runes) <= limit {
		return s
	}
	return string(runes[:limit])
}

// callWithin 给一次控制面调用套上钩子自己的截止时间。
//
// bridge.Client.Call 的超时来自命令表，钩子没有改它的入口；这里用一个到点就
// 走的包装把钩子预算钉在 timeout 上。真超时后后台那次调用会在命令表的时限内
// 自己收尾（关连接即返回），而钩子进程随即 exit，不拖回合。
func callWithin(caller Caller, cmd string, args map[string]any, timeout time.Duration) (json.RawMessage, error) {
	type outcome struct {
		data json.RawMessage
		err  error
	}
	done := make(chan outcome, 1)
	go func() {
		data, err := caller.Call(cmd, args)
		done <- outcome{data, err}
	}()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case out := <-done:
		return out.data, out.err
	case <-timer.C:
		return nil, errors.New("控制面 " + cmd + " 超过钩子预算")
	}
}

// promptSubmit = Python hook_prompt_submit.main：把用户刚说的语音作为本轮的
// 附加上下文注入。没字、引擎没开都原样放行。
func promptSubmit(in io.Reader, out io.Writer, caller Caller) {
	payload := readPayload(in)
	engine.PluginLog("hook_prompt_submit",
		"session", textOf(payload["session_id"]),
		"turn", textOf(payload["turn_id"]),
		"prompt_len", len([]rune(textOf(payload["prompt"]))))

	data, err := callWithin(caller, "take", nil, hookCallTimeout)
	if err != nil {
		engine.PluginLog("hook_prompt_submit_skip", "error", err.Error())
		writeJSONLine(out, continueOutput{Continue: true})
		return
	}
	var taken struct {
		Texts []string `json:"texts"`
	}
	if err := json.Unmarshal(data, &taken); err != nil {
		engine.PluginLog("hook_prompt_submit_skip", "error", err.Error())
		writeJSONLine(out, continueOutput{Continue: true})
		return
	}
	var texts []string
	for _, text := range taken.Texts {
		if strings.TrimSpace(text) != "" {
			texts = append(texts, text)
		}
	}
	if len(texts) == 0 {
		writeJSONLine(out, continueOutput{Continue: true})
		return
	}
	writeJSONLine(out, promptOutput{HookSpecificOutput: promptSpecificOutput{
		HookEventName:     "UserPromptSubmit",
		AdditionalContext: promptPrefix + strings.Join(texts, "\n"),
	}})
}

// stop = Python hook_stop.main：助手说完一轮，把正文交给常驻实例念出来。
// 无论成败都只打一行放行。
func stop(in io.Reader, out io.Writer, caller Caller) {
	payload := readPayload(in)
	text := strings.TrimSpace(textOf(payload["last_assistant_message"]))
	engine.PluginLog("hook_stop",
		"session", textOf(payload["session_id"]),
		"text_len", len([]rune(text)))
	if text == "" {
		// 有些轮次没有正文（工具轮、被打断）。没什么可念的，放行。
		writeJSONLine(out, continueOutput{Continue: true})
		return
	}
	reply, err := callWithin(caller, "speak",
		map[string]any{"text": truncateRunes(text, maxSpeakChars), "auto": true}, hookCallTimeout)
	if err != nil {
		engine.PluginLog("hook_stop_skip", "error", err.Error())
	} else if len(reply) > 0 {
		engine.PluginLog("hook_stop_spoken", "reply", truncateRunes(string(reply), 120))
	}
	writeJSONLine(out, continueOutput{Continue: true})
}

// sessionStart = Python hook_session_start.main：写锚点纸条，确保看门狗在跑。
// 所有异常都吞掉只记日志——拉起引擎失败不该拦住会话。
func sessionStart(in io.Reader, out io.Writer, caller Caller) {
	payload := readPayload(in)
	engine.PluginLog("hook_session_start",
		"session", textOf(payload["session_id"]),
		"source", textOf(payload["source"]))

	// 先挂上放行输出：无论下面发生什么，这一行都要出去。
	defer writeJSONLine(out, continueOutput{Continue: true})

	func() {
		defer func() {
			if r := recover(); r != nil {
				engine.PluginLog("anchor_error", "error", fmt.Sprint(r))
			}
		}()
		engine.WriteAnchor(engine.PluginLog)
	}()

	func() {
		defer func() {
			if r := recover(); r != nil {
				engine.PluginLog("start_error", "error", fmt.Sprint(r))
			}
		}()
		root := engine.Root(engine.PluginLog)
		if !engine.EnsureSupervisor(root, engine.PluginLog) {
			// 已经有人在守，或者拉不起来（自测临时根没有 supervise.py）——都不等。
			return
		}
		prober := func(timeout time.Duration) (json.RawMessage, error) {
			return callWithin(caller, "status", nil, timeout)
		}
		if engine.WaitReady(prober, engine.PluginLog) {
			engine.PluginLog("ready")
		}
	}()
}
