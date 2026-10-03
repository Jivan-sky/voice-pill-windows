package hooks

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"voicepill/internal/engine"
)

// ---------- 假 Caller ----------

type fakeCall struct {
	cmd  string
	args map[string]any
}

type fakeCaller struct {
	mu    sync.Mutex
	calls []fakeCall
	fn    func(cmd string, args map[string]any) (json.RawMessage, error)
}

func (f *fakeCaller) Call(cmd string, args map[string]any) (json.RawMessage, error) {
	f.mu.Lock()
	f.calls = append(f.calls, fakeCall{cmd: cmd, args: args})
	f.mu.Unlock()
	if f.fn != nil {
		return f.fn(cmd, args)
	}
	return json.RawMessage(`{}`), nil
}

func (f *fakeCaller) recorded() []fakeCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]fakeCall(nil), f.calls...)
}

// ---------- 脚手架 ----------

// runHook 跑一个子命令，返回原始 stdout 文本；返回码必须是 0。
func runHook(t *testing.T, kind string, stdin string, caller Caller) string {
	t.Helper()
	var out bytes.Buffer
	if code := run(kind, strings.NewReader(stdin), &out, caller); code != 0 {
		t.Fatalf("%s 的返回码 = %d，想要 0", kind, code)
	}
	return out.String()
}

// assertSingleJSONLine 断言「恰好一行 + 合法 JSON」，并把解析结果交回。
func assertSingleJSONLine(t *testing.T, raw string) map[string]any {
	t.Helper()
	if strings.Count(raw, "\n") != 1 || !strings.HasSuffix(raw, "\n") {
		t.Fatalf("stdout 不是恰好一行：%q", raw)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSuffix(raw, "\n")), &got); err != nil {
		t.Fatalf("stdout 不是合法 JSON：%v（%q）", err, raw)
	}
	return got
}

// isolateHookEnv 把日志、锚点纸条、引擎根、互斥体都挪到测试沙箱里，
// 保证这些用例一个字节都不碰真身。
func isolateHookEnv(t *testing.T) string {
	t.Helper()
	t.Setenv("LOCALAPPDATA", t.TempDir())
	root := t.TempDir()
	t.Setenv(engine.EngineRootEnv, root)
	// 测试专用互斥体名。默认名（engine.SupervisorMutexName）一个字没改。
	t.Setenv(engine.SupervisorMutexEnv, `Local\VoicePill.GoHooksTest-`+filepath.Base(t.TempDir()))
	return root
}

// ---------- 共同契约 ----------

func TestReadPayloadDefaultsToEmptyObject(t *testing.T) {
	for _, raw := range []string{"", "   ", "[]", "null", "not json"} {
		if got := readPayload(strings.NewReader(raw)); len(got) != 0 {
			t.Fatalf("readPayload(%q) = %v，想要空对象", raw, got)
		}
	}
	if got := readPayload(nil); len(got) != 0 {
		t.Fatalf("readPayload(nil) = %v，想要空对象", got)
	}
}

func TestEveryKindExactlyOneJSONLine(t *testing.T) {
	isolateHookEnv(t)
	cases := []struct{ kind, stdin string }{
		{KindSessionStart, `{}`},
		{KindPromptSubmit, `{}`},
		{KindStop, `{}`},
	}
	for _, tc := range cases {
		got := assertSingleJSONLine(t, runHook(t, tc.kind, tc.stdin, &fakeCaller{}))
		if got["continue"] != true {
			t.Fatalf("%s 的输出 = %v，想要 continue:true", tc.kind, got)
		}
	}
}

// ---------- prompt-submit ----------

func TestPromptSubmitEmptyQueue(t *testing.T) {
	fake := &fakeCaller{fn: func(string, map[string]any) (json.RawMessage, error) {
		return json.RawMessage(`{"texts": []}`), nil
	}}
	got := assertSingleJSONLine(t, runHook(t, KindPromptSubmit, `{"session_id":"s1"}`, fake))
	if len(got) != 1 || got["continue"] != true {
		t.Fatalf("空队列应当只打 {\"continue\":true}：%v", got)
	}
	calls := fake.recorded()
	if len(calls) != 1 || calls[0].cmd != "take" || len(calls[0].args) != 0 {
		t.Fatalf("take 调用 = %+v", calls)
	}
}

func TestPromptSubmitInjectsTakenText(t *testing.T) {
	fake := &fakeCaller{fn: func(string, map[string]any) (json.RawMessage, error) {
		return json.RawMessage(`{"texts": ["第一句", "   ", "第二句"]}`), nil
	}}
	raw := runHook(t, KindPromptSubmit, `{}`, fake)
	got := assertSingleJSONLine(t, raw)
	specific, _ := got["hookSpecificOutput"].(map[string]any)
	if specific == nil {
		t.Fatalf("没有 hookSpecificOutput：%v", got)
	}
	if specific["hookEventName"] != "UserPromptSubmit" {
		t.Fatalf("hookEventName = %v", specific["hookEventName"])
	}
	want := promptPrefix + "第一句\n第二句"
	if specific["additionalContext"] != want {
		t.Fatalf("additionalContext = %q，想要 %q", specific["additionalContext"], want)
	}
	// 中文必须是真中文：编码一旦走偏这里会先红。
	if !strings.Contains(raw, "用户刚刚用语音输入（Voice Pill）说了以下内容") {
		t.Fatalf("stdout 里没有真中文前缀：%q", raw)
	}
}

func TestPromptSubmitSwallowsCallError(t *testing.T) {
	fake := &fakeCaller{fn: func(string, map[string]any) (json.RawMessage, error) {
		return nil, errors.New("管道断了")
	}}
	got := assertSingleJSONLine(t, runHook(t, KindPromptSubmit, `{}`, fake))
	if len(got) != 1 || got["continue"] != true {
		t.Fatalf("调用失败应当原样放行：%v", got)
	}
}

func TestPromptSubmitToleratesGarbageStdin(t *testing.T) {
	fake := &fakeCaller{fn: func(string, map[string]any) (json.RawMessage, error) {
		return json.RawMessage(`{"texts":[]}`), nil
	}}
	assertSingleJSONLine(t, runHook(t, KindPromptSubmit, "not json at all", fake))
}

// ---------- stop ----------

func TestStopSilentWhenNoText(t *testing.T) {
	fake := &fakeCaller{}
	got := assertSingleJSONLine(t, runHook(t, KindStop, `{"last_assistant_message":"   "}`, fake))
	if got["continue"] != true {
		t.Fatalf("没有正文应当放行：%v", got)
	}
	if calls := fake.recorded(); len(calls) != 0 {
		t.Fatalf("没有正文不该调 speak：%+v", calls)
	}
}

func TestStopSpeaksRuneTruncatedText(t *testing.T) {
	fake := &fakeCaller{fn: func(string, map[string]any) (json.RawMessage, error) {
		return json.RawMessage(`{"queued":true}`), nil
	}}
	long := strings.Repeat("好", maxSpeakChars+17)
	stdin, err := json.Marshal(map[string]any{"last_assistant_message": "  " + long + "  "})
	if err != nil {
		t.Fatal(err)
	}
	got := assertSingleJSONLine(t, runHook(t, KindStop, string(stdin), fake))
	if got["continue"] != true {
		t.Fatalf("stop 必须放行：%v", got)
	}
	calls := fake.recorded()
	if len(calls) != 1 || calls[0].cmd != "speak" {
		t.Fatalf("speak 调用 = %+v", calls)
	}
	text, _ := calls[0].args["text"].(string)
	if runes := []rune(text); len(runes) != maxSpeakChars {
		t.Fatalf("text 有 %d 个字符，想要 %d", len(runes), maxSpeakChars)
	}
	if !utf8.ValidString(text) {
		t.Fatal("按字符截断之后仍然无效 UTF-8")
	}
	if calls[0].args["auto"] != true {
		t.Fatalf("auto = %v，想要 true", calls[0].args["auto"])
	}
}

func TestStopSwallowsSpeakError(t *testing.T) {
	fake := &fakeCaller{fn: func(string, map[string]any) (json.RawMessage, error) {
		return nil, errors.New("引擎没开")
	}}
	got := assertSingleJSONLine(t, runHook(t, KindStop, `{"last_assistant_message":"念我"}`, fake))
	if got["continue"] != true {
		t.Fatalf("出错也必须放行：%v", got)
	}
}

// ---------- session-start ----------

func TestSessionStartAlwaysOneContinueLine(t *testing.T) {
	root := isolateHookEnv(t)
	got := assertSingleJSONLine(t, runHook(t, KindSessionStart, `{"session_id":"s"}`, &fakeCaller{}))
	if len(got) != 1 || got["continue"] != true {
		t.Fatalf("session-start 应当只打 {\"continue\":true}：%v", got)
	}
	// 临时根里没有 supervise.py：EnsureSupervisor 只会记日志，绝不能拉起任何东西。
	if _, err := os.Stat(filepath.Join(root, "src", "supervise.py")); err == nil {
		t.Fatal("测试根里不该出现 supervise.py")
	}
	if entries, err := os.ReadDir(filepath.Join(root, ".venv")); err == nil && len(entries) > 0 {
		t.Fatalf("测试根里不该出现 .venv：%v", entries)
	}
}
