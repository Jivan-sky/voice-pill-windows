package tools

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type scriptedCaller struct {
	calls   []string
	handler func(cmd string, args map[string]any) (json.RawMessage, error)
}

func (f *scriptedCaller) Call(cmd string, args map[string]any) (json.RawMessage, error) {
	f.calls = append(f.calls, cmd)
	if f.handler == nil {
		return json.RawMessage("{}"), nil
	}
	return f.handler(cmd, args)
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

// structured 把工具结果里的结构化对象解出来；同时要求文本 JSON 与它逐字节一致。
func structured(t *testing.T, res *mcp.CallToolResult) map[string]any {
	t.Helper()
	if res == nil {
		t.Fatal("工具结果是 nil")
	}
	if len(res.Content) != 1 {
		t.Fatalf("Content 长度 = %d，想要 1", len(res.Content))
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("Content[0] 类型 = %T", res.Content[0])
	}
	structuredBytes, err := json.Marshal(res.StructuredContent)
	if err != nil {
		t.Fatalf("marshal StructuredContent: %v", err)
	}
	if string(structuredBytes) != text.Text {
		t.Fatalf("文本 JSON 与结构化对象不一致：\n text=%s\nstruct=%s", text.Text, structuredBytes)
	}
	out := map[string]any{}
	if err := json.Unmarshal(structuredBytes, &out); err != nil {
		t.Fatalf("StructuredContent 不是 JSON 对象：%v", err)
	}
	return out
}

// listen：用户提前按 Fn 松手（status 第二次就 idle）时不该傻等 seconds。
func TestListenStopsWhenUserReleasesEarly(t *testing.T) {
	withFastPoll(t)
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
	res, _, err := h.listen(context.Background(), nil, listenArgs{Seconds: ptr(30)})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("用户提前松手后不该傻等：用了 %v", elapsed)
	}
	if countCalls(fake.calls, "start") != 1 || countCalls(fake.calls, "stop") != 1 {
		t.Fatalf("start/stop 调用次数不对：%v", fake.calls)
	}
	if got := structured(t, res)["text"]; got != "你好" {
		t.Fatalf("text = %v，想要 你好", got)
	}
}

// stop：没在录音就直接取队列，不发 stop。
func TestStopTakesQueueWithoutStopping(t *testing.T) {
	withFastPoll(t)
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
	res, _, err := h.stop(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("stop: %v", err)
	}
	if countCalls(fake.calls, "stop") != 0 {
		t.Fatalf("没在录音就不该发 stop：%v", fake.calls)
	}
	m := structured(t, res)
	if m["text"] != "b" {
		t.Fatalf("text = %v，想要 b", m["text"])
	}
	if texts, ok := m["texts"].([]any); !ok || len(texts) != 2 || texts[0] != "a" {
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
	res, _, err := (&handler{caller: fake}).promptHook(context.Background(), nil, promptHookArgs{})
	if err != nil {
		t.Fatalf("prompt_hook 不该抛：%v", err)
	}
	if structured(t, res)["continue"] != true {
		t.Fatalf("结果 = %s", res.StructuredContent)
	}
}

// prompt_hook：有字时拼 additionalContext，空串丢掉。
func TestPromptHookInjectsPendingText(t *testing.T) {
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return raw(`{"texts":["  ","第一句","第二句"],"count":3}`), nil
	}}
	res, _, err := (&handler{caller: fake}).promptHook(context.Background(), nil, promptHookArgs{SessionID: "s"})
	if err != nil {
		t.Fatalf("prompt_hook: %v", err)
	}
	hook := structured(t, res)["hookSpecificOutput"].(map[string]any)
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
	res, _, err := (&handler{caller: fake}).take(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("take: %v", err)
	}
	m := structured(t, res)
	if m["count"] != float64(1) {
		t.Fatalf("count = %#v", m["count"])
	}
	if texts, ok := m["texts"].([]any); !ok || len(texts) != 1 || texts[0] != "x" {
		t.Fatalf("texts = %#v", m["texts"])
	}
}

// status：探不到就回 running:false + note。
func TestStatusReportsNotRunning(t *testing.T) {
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return nil, errors.New("连不上")
	}}
	res, _, err := (&handler{caller: fake}).status(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("status 不该抛：%v", err)
	}
	m := structured(t, res)
	if m["running"] != false || m["note"] != noteNotRunning {
		t.Fatalf("out = %#v", m)
	}
}

// 八个工具的名字与描述（含中文）必须与 Python 版逐字一致；无参工具入参
// 是 {"type":"object","additionalProperties":false}；listen.seconds 带上
// Python 版有的 title 与 default。
func TestToolListMatchesPythonContract(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	server := mcp.NewServer(&mcp.Implementation{Name: ServerName, Version: ServerVersion}, nil)
	register(server, &scriptedCaller{})
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	done := make(chan error, 1)
	go func() { done <- server.Run(ctx, serverTransport) }()

	client := mcp.NewClient(&mcp.Implementation{Name: "probe", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer session.Close()

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

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("服务端没停下来")
	}
}

// tools/call 的文本 JSON 不做 HTML 转义，和 Python 版 json.dumps 一致。
func TestResultTextKeepsRawHTMLChars(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
		return raw(`{"texts":["<b>&\"引号\""],"count":1}`), nil
	}}
	server := mcp.NewServer(&mcp.Implementation{Name: ServerName, Version: ServerVersion}, nil)
	register(server, fake)
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	done := make(chan error, 1)
	go func() { done <- server.Run(ctx, serverTransport) }()
	client := mcp.NewClient(&mcp.Implementation{Name: "probe", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer session.Close()

	callResult, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "voice_pill_take"})
	if err != nil {
		t.Fatalf("tools/call: %v", err)
	}
	payload, ok := callResult.StructuredContent.(map[string]any)
	if !ok {
		t.Fatalf("StructuredContent 类型 = %T", callResult.StructuredContent)
	}
	texts, ok := payload["texts"].([]any)
	if !ok || len(texts) != 1 || texts[0] != "<b>&\"引号\"" {
		t.Fatalf("StructuredContent 里的字被改过：%#v", payload)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("服务端没停下来")
	}
}

func names(tools []*mcp.Tool) []string {
	out := make([]string, 0, len(tools))
	for _, tool := range tools {
		out = append(out, tool.Name)
	}
	return out
}
