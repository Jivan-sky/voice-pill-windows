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
	if got := out.(map[string]any)["text"]; got != "你好" {
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
	_, out, err := h.stop(context.Background(), nil, noArgs{})
	if err != nil {
		t.Fatalf("stop: %v", err)
	}
	if countCalls(fake.calls, "stop") != 0 {
		t.Fatalf("没在录音就不该发 stop：%v", fake.calls)
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

// status：探不到就回 running:false + note。
func TestStatusReportsNotRunning(t *testing.T) {
	fake := &scriptedCaller{handler: func(cmd string, args map[string]any) (json.RawMessage, error) {
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
}

// 八个工具的名字与描述（含中文）必须与 Python 版逐字一致，且无参工具
// 的入参 schema 是 {"type":"object","additionalProperties":false}。
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
