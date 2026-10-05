// Package tools 把 Voice Pill 的八个 MCP 工具注册到 go-sdk 服务端上。
//
// 工具名与描述逐字对齐 Python 版 plugins\voice-pill\mcp_server.py（描述取
// docstring 原文）；行为走 Caller 接口，便于离线单测。
package tools

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/google/jsonschema-go/jsonschema"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	"voicepill/internal/engine"
)

const (
	// ServerName / ServerVersion 与 Python 版一致。
	ServerName    = "voice_pill"
	ServerVersion = "0.1.1"
)

// transcribeTimeout 对齐 Python 版 TRANSCRIBE_TIMEOUT：引擎 local 后端
// 收尾超时 30 秒，这里留一倍余量。
const transcribeTimeout = 60 * time.Second

// pollInterval 对齐 Python 版 POLL_INTERVAL；单测里会临时调小。
var pollInterval = 300 * time.Millisecond

// statusProbeTimeout 对齐 Python 版 voice_pill_status 的 bridge.probe(timeout=4.0)。
const statusProbeTimeout = 4 * time.Second

// promptHookTakeTimeout 对齐 Python 版 voice_pill_prompt_hook 的
// _call("take", timeout=6.0)。命令表里 take 是 8 秒（那是给语音工具的余量），
// 钩子只要 6 秒；单测里会临时调小。
var promptHookTakeTimeout = 6 * time.Second

// Caller 是控制面客户端在工具层看到的形状。生产实现是 bridge.Client，
// 单测里换成假实现。
type Caller interface {
	Call(cmd string, args map[string]any) (json.RawMessage, error)
	// Probe 用调用方给的时限探一次 status（引擎没在跑就返回错误）。
	Probe(timeout time.Duration) (json.RawMessage, error)
}

// 描述文字：逐字对齐 Python 版 docstring 原文（含换行与缩进）。
const (
	descStatus = "查看 Voice Pill 常驻引擎的状态：是否在运行、当前阶段、后端、热键、待取文字条数。"

	descListen = "让常驻引擎录一段音并返回转写文字。\n\n" +
		"    适用于想直接用语音口述一段内容给我。录音会在 `seconds` 秒后自动结束；\n" +
		"    期间用户自己按 Fn 松手也会提前结束。返回 text 为空表示这段没人说话，\n" +
		"    或麦克风没出声。\n    "

	descStop = "结束正在进行的录音并返回转写文字（若当时没在录音，则返回队列里待取的文字）。"

	descCancel = "丢弃正在进行的录音，不转写、不粘贴。"

	descTake = "取走并清空「待取文字」（用户此前按 Fn 说过的、已经粘贴过的那些）。\n\n" +
		"    取走语义：同一个第二次调用不会重复拿到。用于用户已经用 Fn 说完了、\n" +
		"    再由我来读取内容的场景。只含保鲜期（默认 30 分钟，见\n" +
		"    settings.pending_ttl_minutes）内的字：更早的已经作废、取不回来。\n    "

	descSpeak = "把这段文字用**本机离线 TTS** 念出来（不联网、不需要账号）。\n\n" +
		"    什么时候用：用户希望你「说出来」而不是只打字——比如他正在做别的事、\n" +
		"    或者明确说「念给我听」。念之前会把 markdown 记号去掉、按句切开，\n" +
		"    所以传原文就行，不用自己清理。\n\n" +
		"    注意：这**不是**每轮自动念。默认不自动；要每轮回复都念，由用户在\n" +
		"    `settings.json` 里打开 `speak_replies`。用户说「别念了」时用\n" +
		"    `voice_pill_shutup`。\n    "

	descShutup = "立刻让它闭嘴（打断正在念的话）。\n\n" +
		"    用户按 Fn 也会自动打断——这是\"对话\"而不是\"广播\"的关键。\n    "

	descPromptHook = "给 UserPromptSubmit 钩子用的：把待取文字作为本轮附加上下文注入。\n\n" +
		"    由 Codex 的事件驱动调用，不是给模型主动调的。没有待取文字时原样放行。\n    "
)

const (
	noteNotRunning = "常驻引擎没在跑。调用 voice_pill_listen 会自动把它拉起来。"
	speakEmpty     = "要念的文本是空的。"
	promptPrefix   = "用户刚刚用语音输入（Voice Pill）说了以下内容，请把它当作本轮意图的一部分：\n"
)

type noArgs struct{}

type listenArgs struct {
	Seconds *float64 `json:"seconds,omitempty"`
}

type speakArgs struct {
	Text string `json:"text"`
}

type promptHookArgs struct {
	HookEventName string `json:"hook_event_name,omitempty"`
	SessionID     string `json:"session_id,omitempty"`
	TurnID        string `json:"turn_id,omitempty"`
	Cwd           string `json:"cwd,omitempty"`
	Prompt        string `json:"prompt,omitempty"`
}

// Serve 起 MCP stdio 服务端，直到 stdin 关闭。
// warmUp 是给尺子换的口子：自测不该真去拉引擎。生产里就是 engine.WarmUp。
var warmUp = engine.WarmUp

// startWarmUp 把预热丢到后台。宿主一拉起这个进程就先做掉，别等 SessionStart
// 钩子（实测早 16 秒），也别挡住 initialize——这条路上的 40 毫秒是交付指标。
func startWarmUp() { go warmUp(engine.PluginLog) }

func Serve(caller Caller) error {
	startWarmUp()
	server := mcp.NewServer(&mcp.Implementation{Name: ServerName, Version: ServerVersion}, nil)
	register(server, caller)
	return server.Run(context.Background(), &mcp.StdioTransport{})
}

func register(server *mcp.Server, caller Caller) {
	h := &handler{caller: caller}
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_status", Description: descStatus,
		InputSchema: noArgsSchema("voice_pill_status")}, h.status)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_listen", Description: descListen,
		InputSchema: listenSchema()}, h.listen)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_stop", Description: descStop,
		InputSchema: noArgsSchema("voice_pill_stop")}, h.stop)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_cancel", Description: descCancel,
		InputSchema: noArgsSchema("voice_pill_cancel")}, h.cancel)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_take", Description: descTake,
		InputSchema: noArgsSchema("voice_pill_take")}, h.take)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_speak", Description: descSpeak,
		InputSchema: speakSchema()}, h.speak)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_shutup", Description: descShutup,
		InputSchema: noArgsSchema("voice_pill_shutup")}, h.shutup)
	mcp.AddTool(server, &mcp.Tool{Name: "voice_pill_prompt_hook", Description: descPromptHook,
		InputSchema: promptHookSchema()}, h.promptHook)
}

// mustSchema 用反射推断入参结构，再补上 Python 版 schema 里有的 title/default。
// 结构体标签只能写 description（带 "WORD=" 前缀会被 jsonschema-go 拒绝），
// 所以 title / default 只能这样显式补。
func mustSchema[T any](title string, tune func(*jsonschema.Schema)) *jsonschema.Schema {
	schema, err := jsonschema.For[T](nil)
	if err != nil {
		panic(fmt.Sprintf("推断 %s 的入参 schema 失败：%v", title, err))
	}
	schema.Title = title
	if tune != nil {
		tune(schema)
	}
	return schema
}

func noArgsSchema(name string) *jsonschema.Schema {
	return mustSchema[noArgs](name+"Arguments", nil)
}

func listenSchema() *jsonschema.Schema {
	return mustSchema[listenArgs]("voice_pill_listenArguments", func(s *jsonschema.Schema) {
		if prop := s.Properties["seconds"]; prop != nil {
			prop.Title = "Seconds"
			prop.Default = json.RawMessage("10.0")
			// 对齐 Python 版：可选（缺省补 10）但不是 nullable。指针类型会被
			// 推断成 ["null","number"]，这里抹掉 null。
			prop.Types = nil
			prop.Type = "number"
		}
	})
}

func speakSchema() *jsonschema.Schema {
	return mustSchema[speakArgs]("voice_pill_speakArguments", func(s *jsonschema.Schema) {
		if prop := s.Properties["text"]; prop != nil {
			prop.Title = "Text"
		}
	})
}

func promptHookSchema() *jsonschema.Schema {
	return mustSchema[promptHookArgs]("voice_pill_prompt_hookArguments", func(s *jsonschema.Schema) {
		titles := map[string]string{
			"hook_event_name": "Hook Event Name",
			"session_id":      "Session Id",
			"turn_id":         "Turn Id",
			"cwd":             "Cwd",
			"prompt":          "Prompt",
		}
		for field, title := range titles {
			prop := s.Properties[field]
			if prop == nil {
				continue
			}
			prop.Title = title
			prop.Default = json.RawMessage(`""`)
		}
	})
}

type handler struct {
	caller Caller
}

// result 把一个工具输出包成「一个 TextContent（紧凑 JSON）+ SDK 托管的结构化对象」。
//
// 文本 JSON 用手写 encoder（SetEscapeHTML(false)），和 Python 版 json.dumps 的
// 文本更接近；StructuredContent 交回 SDK 托管（遵守 go-sdk 对 ToolHandlerFor
// 的约定），所以这里只填 Content，把 out 作为 Out 返回。
func result(out map[string]any) (*mcp.CallToolResult, any, error) {
	encoded, err := encodeJSON(out)
	if err != nil {
		return nil, nil, fmt.Errorf("序列化工具结果失败：%v", err)
	}
	return &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{Text: string(encoded)}},
	}, out, nil
}

func encodeJSON(value any) ([]byte, error) {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}

func (h *handler) callMap(cmd string, args map[string]any) (map[string]any, error) {
	raw, err := h.caller.Call(cmd, args)
	if err != nil {
		return nil, err
	}
	return decodeMap(cmd, raw)
}

// callMapWithin 与 callMap 相同，但给这次调用套上工具自己的截止预算。
func (h *handler) callMapWithin(cmd string, args map[string]any, timeout time.Duration) (map[string]any, error) {
	raw, err := callWithin(h.caller, cmd, args, timeout)
	if err != nil {
		return nil, err
	}
	return decodeMap(cmd, raw)
}

// probeMap 用 statusProbeTimeout 探一次 status（对齐 Python 的 bridge.probe）。
func (h *handler) probeMap() (map[string]any, error) {
	raw, err := h.caller.Probe(statusProbeTimeout)
	if err != nil {
		return nil, err
	}
	return decodeMap("status", raw)
}

// ensureEngine 复刻 Python mcp_server._ensure_engine：引擎没在跑就拉起来，
// 返回拉起后的状态。找不到引擎根/启动器时由 engine.Ensure 给出人话错误。
func (h *handler) ensureEngine() (map[string]any, error) {
	raw, err := engine.Ensure(h.caller.Probe, engine.PluginLog)
	if err != nil {
		return nil, err
	}
	return decodeMap("status", raw)
}

func decodeMap(cmd string, raw json.RawMessage) (map[string]any, error) {
	out := map[string]any{}
	if len(raw) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("控制面 %s 回了看不懂的数据：%v", cmd, err)
	}
	return out, nil
}

// callWithin 给一次控制面调用套上工具自己的截止预算。
//
// bridge.Client.Call 的超时来自命令表，工具没有改它的入口；这里本地到点就走。
// 按规矩不往 bridge 上开「谁都能传超时」的口子，也不反向 import internal/hooks。
// 真超时后后台那次调用会在命令表时限内自己收尾（关连接即返回）。
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
		return nil, errors.New("控制面 " + cmd + " 超过工具预算")
	}
}

func (h *handler) status(_ context.Context, _ *mcp.CallToolRequest, _ noArgs) (*mcp.CallToolResult, any, error) {
	state, err := h.probeMap()
	if err != nil {
		return result(map[string]any{"running": false, "note": noteNotRunning})
	}
	out := map[string]any{"running": true}
	for k, v := range state {
		out[k] = v
	}
	return result(out)
}

func (h *handler) listen(_ context.Context, _ *mcp.CallToolRequest, in listenArgs) (*mcp.CallToolResult, any, error) {
	seconds := 10.0
	if in.Seconds != nil {
		seconds = *in.Seconds
	}
	if _, err := h.ensureEngine(); err != nil {
		return nil, nil, err
	}
	if _, err := h.callMap("start", nil); err != nil {
		return nil, nil, err
	}
	deadline := time.Now().Add(time.Duration(math.Max(0.5, seconds) * float64(time.Second)))
	for time.Now().Before(deadline) {
		sleep := pollInterval
		if remaining := time.Until(deadline); remaining < sleep {
			sleep = remaining
		}
		if sleep > 0 {
			time.Sleep(sleep)
		}
		state, err := h.callMap("status", nil)
		if err != nil {
			return nil, nil, err
		}
		if phaseOf(state) == "idle" {
			break
		}
	}
	if _, err := h.callMap("stop", nil); err != nil {
		return nil, nil, err
	}
	return h.waitForResult()
}

func (h *handler) stop(_ context.Context, _ *mcp.CallToolRequest, _ noArgs) (*mcp.CallToolResult, any, error) {
	// Python 版这里用的是 _ensure_engine() 的返回值判 phase。
	state, err := h.ensureEngine()
	if err != nil {
		return nil, nil, err
	}
	if phaseOf(state) != "idle" {
		if _, err := h.callMap("stop", nil); err != nil {
			return nil, nil, err
		}
	}
	return h.waitForResult()
}

func (h *handler) cancel(_ context.Context, _ *mcp.CallToolRequest, _ noArgs) (*mcp.CallToolResult, any, error) {
	if _, err := h.callMap("cancel", nil); err != nil {
		return nil, nil, err
	}
	state, err := h.callMap("status", nil)
	if err != nil {
		return nil, nil, err
	}
	return result(state)
}

func (h *handler) take(_ context.Context, _ *mcp.CallToolRequest, _ noArgs) (*mcp.CallToolResult, any, error) {
	data, err := h.callMap("take", nil)
	if err != nil {
		return nil, nil, err
	}
	return result(data)
}

func (h *handler) speak(_ context.Context, _ *mcp.CallToolRequest, in speakArgs) (*mcp.CallToolResult, any, error) {
	if strings.TrimSpace(in.Text) == "" {
		return nil, nil, errors.New(speakEmpty)
	}
	data, err := h.callMap("speak", map[string]any{"text": in.Text, "auto": false})
	if err != nil {
		return nil, nil, err
	}
	return result(data)
}

func (h *handler) shutup(_ context.Context, _ *mcp.CallToolRequest, _ noArgs) (*mcp.CallToolResult, any, error) {
	data, err := h.callMap("shutup", nil)
	if err != nil {
		return nil, nil, err
	}
	return result(data)
}

func (h *handler) promptHook(_ context.Context, _ *mcp.CallToolRequest, _ promptHookArgs) (*mcp.CallToolResult, any, error) {
	taken, err := h.callMapWithin("take", nil, promptHookTakeTimeout)
	if err != nil {
		// 钩子绝不能因为引擎没开就拦住用户的这一轮对话。
		return result(map[string]any{"continue": true})
	}
	texts := stringSlice(taken["texts"])
	kept := make([]string, 0, len(texts))
	for _, text := range texts {
		if strings.TrimSpace(text) != "" {
			kept = append(kept, text)
		}
	}
	if len(kept) == 0 {
		return result(map[string]any{"continue": true})
	}
	return result(map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":     "UserPromptSubmit",
			"additionalContext": promptPrefix + strings.Join(kept, "\n"),
		},
	})
}

// waitForResult 等这次录音走完（不再处于 recording/transcribing），然后把文字取走。
func (h *handler) waitForResult() (*mcp.CallToolResult, any, error) {
	deadline := time.Now().Add(transcribeTimeout)
	var state map[string]any
	for time.Now().Before(deadline) {
		current, err := h.callMap("status", nil)
		if err != nil {
			return nil, nil, err
		}
		state = current
		if phaseOf(current) == "idle" {
			break
		}
		time.Sleep(pollInterval)
	}
	taken, err := h.callMap("take", nil)
	if err != nil {
		return nil, nil, err
	}
	texts := stringSlice(taken["texts"])
	text := ""
	if len(texts) > 0 {
		text = texts[len(texts)-1]
	}
	var phase any
	if state != nil {
		phase = state["phase"]
	}
	return result(map[string]any{
		"text":      text,
		"texts":     texts,
		"timed_out": state != nil && phaseOf(state) != "idle",
		"phase":     phase,
	})
}

func phaseOf(state map[string]any) string {
	if state == nil {
		return ""
	}
	phase, _ := state["phase"].(string)
	return phase
}

func stringSlice(value any) []string {
	list, ok := value.([]any)
	if !ok {
		return []string{}
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if text, ok := item.(string); ok {
			out = append(out, text)
		}
	}
	return out
}
