package tools

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"voicepill/internal/engine"
)

type scriptedCaller struct {
	calls        []string
	probes       []time.Duration
	events       []string
	handler      func(cmd string, args map[string]any) (json.RawMessage, error)
	probeHandler func(timeout time.Duration) (json.RawMessage, error)
}

func (f *scriptedCaller) Call(cmd string, args map[string]any) (json.RawMessage, error) {
	f.calls = append(f.calls, cmd)
	f.events = append(f.events, "call:"+cmd)
	if f.handler == nil {
		return json.RawMessage("{}"), nil
	}
	return f.handler(cmd, args)
}

// Probe 默认回 idle（引擎在跑）：大多数用例只关心 Ensure 之后的那条链路。
func (f *scriptedCaller) Probe(timeout time.Duration) (json.RawMessage, error) {
	f.probes = append(f.probes, timeout)
	f.events = append(f.events, "probe")
	if f.probeHandler == nil {
		return json.RawMessage(`{"phase":"idle"}`), nil
	}
	return f.probeHandler(timeout)
}

// isolateEngine 把引擎根与 LOCALAPPDATA 挪进沙箱：探不到引擎时 engine.Ensure
// 只会走到「找不到启动器」就返回，绝不拉起任何东西，也不碰真身的纸条/密钥。
func isolateEngine(t *testing.T) {
	t.Helper()
	t.Setenv("LOCALAPPDATA", t.TempDir())
	t.Setenv("VOICEPILL_ENGINE_ROOT", t.TempDir())
}

func raw(s string) json.RawMessage { return json.RawMessage(s) }

func ptr(value float64) *float64 { return &value }

func withFastPoll(t *testing.T) {
	t.Helper()
	old := pollInterval
	pollInterval = time.Millisecond
	t.Cleanup(func() { pollInterval = old })
}

func countCalls(calls []string, cmd string) int {
	n := 0
	for _, c := range calls {
		if c == cmd {
			n++
		}
	}
	return n
}

// listen：用户提前按 Fn 松手（status 第二次就 idle）时不该傻等 seconds。
func TestListenStopsWhenUserReleasesEarly(t *testing.T) {
	withFastPoll(t)
	isolateEngine(t)
	statusCalls := 0
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		switch cmd {
		case "start", "stop":
			return raw("{}"), nil
		case "status":
			statusCalls++
			if statusCalls == 1 {
				return raw(`{"phase":"recording"}`), nil
			}
			return raw(`{"phase":"idle"}`), nil
		case "take":
			return raw(`{"texts":["你好"],"count":1}`), nil
		}
		return raw("{}"), nil
	}}
	h := &handler{caller: fake}
	started := time.Now()
	_, out, err := h.listen(context.Background(), nil, listenArgs{Seconds: ptr(30)})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("用户提前松手后不该傻等：用了 %v", elapsed)
	}
	if countCalls(fake.calls, "start") != 1 || countCalls(fake.calls, "stop") != 1 {
		t.Fatalf("start/stop 调用次数不对：%v", fake.calls)
	}
	// 链路顺序必须是 Ensure（探一次 status）→ start …，别跳过接线。
	if len(fake.events) < 2 || fake.events[0] != "probe" || fake.events[1] != "call:start" {
		t.Fatalf("事件序 = %v，想要 probe 在最前、紧接 call:start", fake.events)
	}
	if len(fake.probes) != 1 || fake.probes[0] != statusProbeTimeout {
		t.Fatalf("Ensure 的探针预算 = %v，想要 %v", fake.probes, statusProbeTimeout)
	}
	if got := out.(map[string]any)["text"]; got != "你好" {
		t.Fatalf("text = %v，想要 你好", got)
	}
}

// stop：没在录音就直接取队列，不发 stop。
func TestStopTakesQueueWithoutStopping(t *testing.T) {
	withFastPoll(t)
	isolateEngine(t)
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		switch cmd {
		case "status":
			return raw(`{"phase":"idle"}`), nil
		case "take":
			return raw(`{"texts":["a","b"],"count":2}`), nil
		}
		return raw("{}"), nil
	}}
	h := &handler{caller: fake}
	_, out, err := h.stop(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("stop: %v", err)
	}
	if countCalls(fake.calls, "stop") != 0 {
		t.Fatalf("没在录音就不该发 stop：%v", fake.calls)
	}
	if len(fake.probes) != 1 {
		t.Fatalf("stop 应当先 _ensure_engine（探一次）：%v", fake.events)
	}
	m := out.(map[string]any)
	if m["text"] != "b" {
		t.Fatalf("text = %v，想要 b", m["text"])
	}
	if texts, ok := m["texts"].([]string); !ok || len(texts) != 2 || texts[0] != "a" {
		t.Fatalf("texts = %#v", m["texts"])
	}
	if m["timed_out"] != false {
		t.Fatalf("timed_out = %v，想要 false", m["timed_out"])
	}
}

// speak：空文本直接报错，不碰控制面。
func TestSpeakRejectsEmptyText(t *testing.T) {
	fake := &scriptedCaller{}
	_, _, err := (&handler{caller: fake}).speak(context.Background(), nil, speakArgs{Text: "   "})
	if err == nil || err.Error() != speakEmpty {
		t.Fatalf("err = %v，想要 %q", err, speakEmpty)
	}
	if len(fake.calls) != 0 {
		t.Fatalf("空文本不该发命令：%v", fake.calls)
	}
}

// speak：非空文本带 auto=false 发出去。
func TestSpeakSendsAutoFalse(t *testing.T) {
	var got map[string]any
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		got = args
		return raw(`{"speaking":true}`), nil
	}}
	_, _, err := (&handler{caller: fake}).speak(context.Background(), nil, speakArgs{Text: "念这句"})
	if err != nil {
		t.Fatalf("speak: %v", err)
	}
	if got["text"] != "念这句" || got["auto"] != false {
		t.Fatalf("args = %#v", got)
	}
}

// prompt_hook：控制面异常一律吞掉，回 continue:true。
func TestPromptHookSwallowsCallerErrors(t *testing.T) {
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return nil, errors.New("控制面没在跑")
	}}
	_, out, err := (&handler{caller: fake}).promptHook(context.Background(), nil, promptHookArgs{})
	if err != nil {
		t.Fatalf("prompt_hook 不该抛：%v", err)
	}
	if out.(map[string]any)["continue"] != true {
		t.Fatalf("out = %#v", out)
	}
}

// prompt_hook：有字时拼 additionalContext，空串丢掉。
func TestPromptHookInjectsPendingText(t *testing.T) {
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return raw(`{"texts":["  ","第一句","第二句"],"count":3}`), nil
	}}
	_, out, err := (&handler{caller: fake}).promptHook(context.Background(), nil, promptHookArgs{SessionID: "s"})
	if err != nil {
		t.Fatalf("prompt_hook: %v", err)
	}
	hook := out.(map[string]any)["hookSpecificOutput"].(map[string]any)
	if hook["hookEventName"] != "UserPromptSubmit" {
		t.Fatalf("hookEventName = %v", hook["hookEventName"])
	}
	if hook["additionalContext"] != promptPrefix+"第一句\n第二句" {
		t.Fatalf("additionalContext = %q", hook["additionalContext"])
	}
}

// take：取走语义，原样透传，不重复取。
func TestTakePassesThrough(t *testing.T) {
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return raw(`{"texts":["x"],"count":1}`), nil
	}}
	_, out, err := (&handler{caller: fake}).take(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("take: %v", err)
	}
	m := out.(map[string]any)
	if m["count"] != float64(1) {
		t.Fatalf("count = %#v", m["count"])
	}
	if texts, ok := m["texts"].([]any); !ok || len(texts) != 1 || texts[0] != "x" {
		t.Fatalf("texts = %#v", m["texts"])
	}
}

// status：探不到就回 running:false + note；探测走 Probe(timeout=4.0)，
// 不发 status 命令（对齐 Python voice_pill_status 的 bridge.probe(timeout=4.0)）。
func TestStatusReportsNotRunning(t *testing.T) {
	isolateEngine(t)
	fake := &scriptedCaller{probeHandler: func(time.Duration) (json.RawMessage, error) {
		return nil, errors.New("连不上")
	}}
	_, out, err := (&handler{caller: fake}).status(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("status 不该抛：%v", err)
	}
	m := out.(map[string]any)
	if m["running"] != false || m["note"] != noteNotRunning {
		t.Fatalf("out = %#v", m)
	}
	if len(fake.calls) != 0 {
		t.Fatalf("status 不该发命令：%v", fake.calls)
	}
	if len(fake.probes) != 1 || fake.probes[0] != 4*time.Second {
		t.Fatalf("status 探测预算 = %v，想要 [4s]", fake.probes)
	}
}

// status：探到引擎就把 phase 等字段一起带回来。
func TestStatusReportsRunning(t *testing.T) {
	fake := &scriptedCaller{probeHandler: func(time.Duration) (json.RawMessage, error) {
		return raw(`{"phase":"idle","pid":4242,"backend":"local"}`), nil
	}}
	_, out, err := (&handler{caller: fake}).status(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	m := out.(map[string]any)
	if m["running"] != true || m["phase"] != "idle" || m["pid"] != float64(4242) {
		t.Fatalf("out = %#v", m)
	}
}

// listen：拉不起引擎时报错、且不把 start 发出去。
func TestListenEnsureFailureSkipsStart(t *testing.T) {
	withFastPoll(t)
	isolateEngine(t)
	fake := &scriptedCaller{probeHandler: func(time.Duration) (json.RawMessage, error) {
		return nil, errors.New("连不上")
	}}
	_, _, err := (&handler{caller: fake}).listen(context.Background(), nil, listenArgs{Seconds: ptr(30)})
	if err == nil {
		t.Fatal("拉不起引擎应当报错")
	}
	if countCalls(fake.calls, "start") != 0 {
		t.Fatalf("引擎没起来就不该 start：%v", fake.calls)
	}
}

// prompt_hook 的 take 有 6 秒预算：装死的控制面不能让钩子一直等。
func TestPromptHookTakeBudget(t *testing.T) {
	if promptHookTakeTimeout != 6*time.Second {
		t.Fatalf("promptHookTakeTimeout = %v，想要 6s（对齐 Python _call(\"take\", timeout=6.0)）", promptHookTakeTimeout)
	}
	old := promptHookTakeTimeout
	promptHookTakeTimeout = 30 * time.Millisecond
	t.Cleanup(func() { promptHookTakeTimeout = old })

	// 装死 5 秒：预算若失效，这个用例 5 秒后带着「用了 5s」判红，而不是挂死。
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		time.Sleep(5 * time.Second)
		return raw(`{"texts":["迟到"]}`), nil
	}}
	started := time.Now()
	_, out, err := (&handler{caller: fake}).promptHook(context.Background(), nil, promptHookArgs{})
	if err != nil {
		t.Fatalf("prompt_hook 不该抛：%v", err)
	}
	if out.(map[string]any)["continue"] != true {
		t.Fatalf("out = %#v", out)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("预算没生效，用了 %v", elapsed)
	}
}

// newPair 起一对进程内的 MCP 会话；server->client 的原始字节被采集下来。
func newPair(t *testing.T, ctx context.Context, caller Caller) (*mcp.ClientSession, *captureWriter) {
	t.Helper()
	server := mcp.NewServer(&mcp.Implementation{Name: ServerName, Version: ServerVersion}, nil)
	register(server, caller)

	serverReader, clientWriter := io.Pipe()
	clientReader, serverWriter := io.Pipe()
	capture := &captureWriter{w: serverWriter}
	go func() { _ = server.Run(ctx, &mcp.IOTransport{Reader: serverReader, Writer: capture}) }()

	client := mcp.NewClient(&mcp.Implementation{Name: "probe", Version: "0"}, nil)
	session, err := client.Connect(ctx, &mcp.IOTransport{Reader: clientReader, Writer: clientWriter}, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { session.Close() })
	return session, capture
}

type captureWriter struct {
	mu  sync.Mutex
	buf bytes.Buffer
	w   io.WriteCloser
}

func (c *captureWriter) Write(p []byte) (int, error) {
	c.mu.Lock()
	c.buf.Write(p)
	c.mu.Unlock()
	return c.w.Write(p)
}

func (c *captureWriter) Close() error { return c.w.Close() }

func (c *captureWriter) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.String()
}

// 八个工具的名字与描述（含中文）必须与 Python 版逐字一致；无参工具入参
// 是 {"type":"object","additionalProperties":false}；listen.seconds 带上
// Python 版有的 title/default，且类型是纯 number（不是 ["null","number"]）。
func TestToolListMatchesPythonContract(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	session, _ := newPair(t, ctx, &scriptedCaller{})

	result, err := session.ListTools(ctx, &mcp.ListToolsParams{})
	if err != nil {
		t.Fatalf("tools/list: %v", err)
	}
	want := map[string]string{
		"voice_pill_status":      descStatus,
		"voice_pill_listen":      descListen,
		"voice_pill_stop":        descStop,
		"voice_pill_cancel":      descCancel,
		"voice_pill_take":        descTake,
		"voice_pill_speak":       descSpeak,
		"voice_pill_shutup":      descShutup,
		"voice_pill_prompt_hook": descPromptHook,
	}
	if len(result.Tools) != len(want) {
		t.Fatalf("工具数 = %d，想要 %d", len(result.Tools), len(want))
	}
	byName := map[string]*mcp.Tool{}
	for _, tool := range result.Tools {
		byName[tool.Name] = tool
	}
	for name, desc := range want {
		tool, ok := byName[name]
		if !ok {
			t.Fatalf("缺工具 %s（拿到 %v）", name, names(result.Tools))
		}
		if tool.Description != desc {
			t.Fatalf("%s 描述不一致：\n got %q\nwant %q", name, tool.Description, desc)
		}
	}
	if !strings.Contains(byName["voice_pill_status"].Description, "状态") {
		t.Fatalf("中文被编坏了：%q", byName["voice_pill_status"].Description)
	}

	schema, ok := byName["voice_pill_status"].InputSchema.(map[string]any)
	if !ok {
		t.Fatalf("InputSchema 类型 = %T", byName["voice_pill_status"].InputSchema)
	}
	if schema["type"] != "object" || schema["additionalProperties"] != false {
		t.Fatalf("无参工具 schema = %#v", schema)
	}
	if schema["title"] != "voice_pill_statusArguments" {
		t.Fatalf("无参工具 schema title = %#v", schema["title"])
	}

	listen, ok := byName["voice_pill_listen"].InputSchema.(map[string]any)
	if !ok {
		t.Fatalf("listen InputSchema 类型 = %T", byName["voice_pill_listen"].InputSchema)
	}
	if listen["title"] != "voice_pill_listenArguments" {
		t.Fatalf("listen schema title = %#v", listen["title"])
	}
	seconds, ok := listen["properties"].(map[string]any)["seconds"].(map[string]any)
	if !ok {
		t.Fatalf("listen.seconds schema = %#v", listen["properties"])
	}
	if seconds["title"] != "Seconds" || seconds["default"] != float64(10) {
		t.Fatalf("listen.seconds = %#v", seconds)
	}
	if seconds["type"] != "number" {
		t.Fatalf("listen.seconds 类型 = %#v，想要 number", seconds["type"])
	}
}

// tools/call：文本 JSON 手写、不转义；StructuredContent 交给 SDK 托管
// （线上原始字节带 HTML 转义），但按 JSON 解析后与文本等价。
func TestWireTextRawAndStructuredEscaped(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return raw(`{"texts":["<b>&\"引号\""],"count":1}`), nil
	}}
	session, capture := newPair(t, ctx, fake)

	callResult, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "voice_pill_take"})
	if err != nil {
		t.Fatalf("tools/call: %v", err)
	}

	text, ok := callResult.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("Content[0] 类型 = %T", callResult.Content[0])
	}
	if strings.Contains(text.Text, `\u003c`) || strings.Contains(text.Text, `\u0026`) {
		t.Fatalf("文本里出现 HTML 转义序列：%s", text.Text)
	}
	if !strings.Contains(text.Text, `<b>&`) {
		t.Fatalf("文本被改过：%s", text.Text)
	}

	// 解析后的结构化对象与文本等价。
	var fromText map[string]any
	if err := json.Unmarshal([]byte(text.Text), &fromText); err != nil {
		t.Fatalf("文本不是 JSON：%v", err)
	}
	structured, ok := callResult.StructuredContent.(map[string]any)
	if !ok {
		t.Fatalf("StructuredContent 类型 = %T", callResult.StructuredContent)
	}
	if !equalJSON(fromText, structured) {
		t.Fatalf("解析后不等价：\n text=%#v\nstruct=%#v", fromText, structured)
	}

	// 线上原始帧：SDK 托管的 StructuredContent 一定带 HTML 转义。
	frame := ""
	for _, line := range strings.Split(capture.String(), "\n") {
		if strings.Contains(line, `"structuredContent"`) {
			frame = line
			break
		}
	}
	if frame == "" {
		t.Fatalf("线上没抓到带 structuredContent 的响应帧")
	}
	if !strings.Contains(frame, `\u003c`) {
		t.Fatalf("StructuredContent 线上应带 HTML 转义：%s", frame)
	}
}

func equalJSON(a, b any) bool {
	left, err1 := json.Marshal(a)
	right, err2 := json.Marshal(b)
	return err1 == nil && err2 == nil && string(left) == string(right)
}

func names(tools []*mcp.Tool) []string {
	out := make([]string, 0, len(tools))
	for _, tool := range tools {
		out = append(out, tool.Name)
	}
	return out
}

// ---------- 预热（宿主刚把插件拉起来那一刻）----------

// 尺子只验「有没有把预热丢出去」，预热本身归 engine 包的尺子管。
func TestStartWarmUpRunsInBackground(t *testing.T) {
	old := warmUp
	done := make(chan struct{})
	warmUp = func(engine.Logger) { close(done) }
	defer func() { warmUp = old }()

	startWarmUp()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("startWarmUp 没把预热跑起来")
	}
}
