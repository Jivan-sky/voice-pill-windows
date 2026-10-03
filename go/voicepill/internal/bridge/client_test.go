package bridge

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

// 金标准向量：key = 字节 0..31，msg = ASCII 的 32 个 "0"。
// 该值已用 Python hmac/hashlib 独立复算过。
func TestAuthTokenGoldenVector(t *testing.T) {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
	}
	got := AuthToken(key, strings.Repeat("0", 32))
	want := "9f8f7fbc3e6c1c6192a185bc2305596bb4681df6bb51f544f37c373679a5fe6f"
	if got != want {
		t.Fatalf("AuthToken = %q，想要 %q", got, want)
	}
}

// VOICEPILL_JSON_PIPE_NAME 非空时整个覆盖派生结果。
func TestPipeNameOverride(t *testing.T) {
	const override = `\\.\pipe\VoicePill-Json-deadbeef`
	t.Setenv(JSONPipeNameEnv, override)
	if got := PipeName(); got != override {
		t.Fatalf("PipeName = %q，想要 %q", got, override)
	}
}

// 派生规则：\\.\pipe\VoicePill-Json- + sha256(normcase(引擎目录))[:16]。
// 期望值已用 Python hashlib + os.path.normcase 独立复算过。
func TestPipeNameForGolden(t *testing.T) {
	got := PipeNameFor(`C:\vp-test\VoicePill`)
	want := JSONPipePrefix + "6f79cde14a95df56"
	if got != want {
		t.Fatalf("PipeNameFor = %q，想要 %q", got, want)
	}
}

// normcase：大小写与正斜杠都要归一，和 Python os.path.normcase 一致。
func TestPipeNameForNormcase(t *testing.T) {
	base := PipeNameFor(`C:\vp-test\VoicePill`)
	if other := PipeNameFor(`C:\VP-Test\VoicePill`); other != base {
		t.Fatalf("大小写没归一：%q != %q", other, base)
	}
	if other := PipeNameFor(`C:/vp-test/voicepill`); other != base {
		t.Fatalf("正斜杠没归一：%q != %q", other, base)
	}
}

// 分帧：恰好 MaxRequestBytes 的一行收下，多一个字节就拒绝。
func TestReadLineLimit(t *testing.T) {
	okLine := strings.Repeat("a", MaxRequestBytes) + "\n"
	line, err := readLine(bufio.NewReader(strings.NewReader(okLine)))
	if err != nil {
		t.Fatalf("恰好 %d 字节的行不该被拒：%v", MaxRequestBytes, err)
	}
	if len(line) != MaxRequestBytes {
		t.Fatalf("行长 = %d，想要 %d", len(line), MaxRequestBytes)
	}

	bigLine := strings.Repeat("a", MaxRequestBytes+1) + "\n"
	if _, err := readLine(bufio.NewReader(strings.NewReader(bigLine))); err == nil {
		t.Fatalf("超过 %d 字节的行应该被拒绝", MaxRequestBytes)
	}
}

// 分帧：读得下的行原样返回，CRLF 也不算进内容。
func TestReadLineFrames(t *testing.T) {
	line, err := readLine(bufio.NewReader(strings.NewReader("{\"ok\":true}\n")))
	if err != nil {
		t.Fatalf("readLine: %v", err)
	}
	if string(line) != `{"ok":true}` {
		t.Fatalf("line = %q", line)
	}
	line, err = readLine(bufio.NewReader(strings.NewReader("{\"a\":1}\r\n")))
	if err != nil {
		t.Fatalf("readLine: %v", err)
	}
	if string(line) != `{"a":1}` {
		t.Fatalf("line = %q", line)
	}
}

// ---------- 绝对钉子 ----------

// MaxRequestBytes 与 Python 侧 tools\bridge-json-selftest.py 的钉子对称：
// 请求上限就是 64 KiB，谁偷偷放大这条就红。
func TestMaxRequestBytesPinned(t *testing.T) {
	if MaxRequestBytes != 64*1024 {
		t.Fatalf("MaxRequestBytes = %d，钉死在 64 KiB", MaxRequestBytes)
	}
}

// 命令表就是 Python mcp_server._call 的取值，别偷偷放大。
func TestCommandTimeoutsPinned(t *testing.T) {
	want := map[string]time.Duration{
		"status": 8 * time.Second,
		"start":  15 * time.Second,
		"stop":   15 * time.Second,
		"cancel": 15 * time.Second,
		"take":   8 * time.Second,
		"speak":  10 * time.Second,
		"shutup": 8 * time.Second,
	}
	if len(commandTimeouts) != len(want) {
		t.Fatalf("命令表有 %d 条，想要 %d 条", len(commandTimeouts), len(want))
	}
	for cmd, timeout := range want {
		if commandTimeouts[cmd] != timeout {
			t.Fatalf("%s 超时 = %v，想要 %v", cmd, commandTimeouts[cmd], timeout)
		}
	}
	if len(Commands) != len(want) {
		t.Fatalf("Commands 有 %d 条，想要 %d 条", len(Commands), len(want))
	}
	for _, cmd := range Commands {
		if _, ok := want[cmd]; !ok {
			t.Fatalf("Commands 里多出 %q", cmd)
		}
	}
}

// ---------- 测试内的命名管道桩（测试专用名字，绝不碰真身） ----------

// stubConn 是桩服务端这一侧：直接读写命名管道句柄。
type stubConn struct {
	h    windows.Handle
	rbuf []byte
	eof  bool
}

func (c *stubConn) readLine() ([]byte, error) {
	for {
		if i := bytes.IndexByte(c.rbuf, '\n'); i >= 0 {
			line := bytes.TrimRight(c.rbuf[:i], "\r")
			c.rbuf = c.rbuf[i+1:]
			return line, nil
		}
		if c.eof {
			if len(c.rbuf) == 0 {
				return nil, io.EOF
			}
			line := c.rbuf
			c.rbuf = nil
			return line, nil
		}
		chunk := make([]byte, 4096)
		var n uint32
		err := windows.ReadFile(c.h, chunk, &n, nil)
		if err != nil {
			if errors.Is(err, windows.ERROR_BROKEN_PIPE) || errors.Is(err, windows.ERROR_NO_DATA) {
				c.eof = true
				continue
			}
			return nil, err
		}
		c.rbuf = append(c.rbuf, chunk[:n]...)
	}
}

func (c *stubConn) write(b []byte) error {
	for len(b) > 0 {
		var n uint32
		if err := windows.WriteFile(c.h, b, &n, nil); err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}

func (c *stubConn) send(v any) error {
	raw, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return c.write(append(raw, '\n'))
}

// stubPipe 是测试专用的命名管道服务端。
type stubPipe struct {
	handle windows.Handle
	name   string
}

var stubSeq atomic.Int64

func newStubPipe(t *testing.T) *stubPipe {
	t.Helper()
	name := fmt.Sprintf(`\\.\pipe\VoicePill-Json-test-%d-%d`, os.Getpid(), stubSeq.Add(1))
	namePtr, err := windows.UTF16PtrFromString(name)
	if err != nil {
		t.Fatal(err)
	}
	handle, err := windows.CreateNamedPipe(namePtr,
		windows.PIPE_ACCESS_DUPLEX,
		windows.PIPE_TYPE_BYTE|windows.PIPE_READMODE_BYTE|windows.PIPE_WAIT,
		windows.PIPE_UNLIMITED_INSTANCES,
		1<<16, 1<<16, 0, nil)
	if err != nil {
		t.Fatalf("CreateNamedPipe: %v", err)
	}
	t.Cleanup(func() { windows.CloseHandle(handle) })
	return &stubPipe{handle: handle, name: name}
}

// accept 等客户端连上（最多 5 秒），返回服务端这一侧的连接。
func (s *stubPipe) accept(t *testing.T) *stubConn {
	t.Helper()
	type result struct {
		conn *stubConn
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		err := windows.ConnectNamedPipe(s.handle, nil)
		if err != nil && !errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			ch <- result{nil, err}
			return
		}
		ch <- result{&stubConn{h: s.handle}, nil}
	}()
	select {
	case r := <-ch:
		if r.err != nil {
			t.Fatalf("ConnectNamedPipe: %v", r.err)
		}
		return r.conn
	case <-time.After(5 * time.Second):
		t.Fatal("客户端没连上测试桩")
		return nil
	}
}

// ---------- 测试脚手架 ----------

type callOutcome struct {
	data    json.RawMessage
	err     error
	elapsed time.Duration
}

// callAsync 在后台打一次 call()，把结果与耗时送回。
func callAsync(pipe, cmd string, args map[string]any, timeout time.Duration) <-chan callOutcome {
	ch := make(chan callOutcome, 1)
	go func() {
		start := time.Now()
		data, err := call(pipe, cmd, args, timeout)
		ch <- callOutcome{data, err, time.Since(start)}
	}()
	return ch
}

// useTempKey 把密钥落进临时 LOCALAPPDATA，返回这次要用的 key。
func useTempKey(t *testing.T) []byte {
	t.Helper()
	t.Setenv("LOCALAPPDATA", t.TempDir())
	key, err := LoadOrCreateKey()
	if err != nil {
		t.Fatalf("LoadOrCreateKey: %v", err)
	}
	return key
}

// handshake 走「发 nonce → 读 auth → 读请求」，顺带核对 HMAC。
func handshake(t *testing.T, conn *stubConn, key []byte, nonce string) string {
	t.Helper()
	if err := conn.send(map[string]any{"nonce": nonce}); err != nil {
		t.Fatalf("发 nonce: %v", err)
	}
	authLine, err := conn.readLine()
	if err != nil {
		t.Fatalf("读 auth: %v", err)
	}
	var authMsg struct {
		Auth string `json:"auth"`
	}
	if err := json.Unmarshal(authLine, &authMsg); err != nil {
		t.Fatalf("auth 不是 JSON：%v（%q）", err, authLine)
	}
	if want := AuthToken(key, nonce); authMsg.Auth != want {
		t.Fatalf("auth=%q，想要 %q", authMsg.Auth, want)
	}
	reqLine, err := conn.readLine()
	if err != nil {
		t.Fatalf("读请求: %v", err)
	}
	return string(reqLine)
}

func assertBridgeError(t *testing.T, err error, pipe, wantSub string) {
	t.Helper()
	if err == nil {
		t.Fatalf("想要错误（含 %q），拿到 nil", wantSub)
	}
	var be *BridgeError
	if !errors.As(err, &be) {
		t.Fatalf("错误类型 = %T，想要 *BridgeError：%v", err, err)
	}
	if !strings.Contains(err.Error(), pipe) {
		t.Fatalf("错误文本没带管道名 %q：%v", pipe, err)
	}
	if !strings.Contains(err.Error(), wantSub) {
		t.Fatalf("错误文本没含 %q：%v", wantSub, err)
	}
}

// parseRequest 解析请求行，返回 cmd、args 原文与顶层键数。
//
// 只认语义：cmd 必须是字符串，args 必须原样是 {}（null 不行）。
// 键序不钉字节——Python 侧 _parse 也不看键序。
func parseRequest(t *testing.T, line string) (cmd string, args string, keys int) {
	t.Helper()
	var top map[string]json.RawMessage
	if err := json.Unmarshal([]byte(line), &top); err != nil {
		t.Fatalf("请求行不是 JSON 对象：%v（%q）", err, line)
	}
	if err := json.Unmarshal(top["cmd"], &cmd); err != nil {
		t.Fatalf("cmd 不是字符串：%v（%q）", err, line)
	}
	return cmd, string(top["args"]), len(top)
}

const stubNonce = "0123456789abcdef0123456789abcdef"

// ---------- Call / exchange 的真覆盖 ----------

// 走真命名管道的一问一答：nil 的 args 要序列化成 {}，data 原样回。
func TestCallRoundTrip(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status", nil, 3*time.Second)
	conn := stub.accept(t)
	request := handshake(t, conn, key, stubNonce)
	if cmd, args, keys := parseRequest(t, request); cmd != "status" || args != "{}" || keys != 2 {
		t.Fatalf("请求行 = %q（cmd=%q args=%s 顶层键=%d）", request, cmd, args, keys)
	}
	if err := conn.send(map[string]any{"ok": true, "data": map[string]any{"pid": 4242, "phase": "idle"}}); err != nil {
		t.Fatalf("回结果: %v", err)
	}
	out := <-result
	if out.err != nil {
		t.Fatalf("call: %v", out.err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.data, &got); err != nil {
		t.Fatalf("data 不是 JSON：%v（%s）", err, out.data)
	}
	if got["pid"] != float64(4242) || got["phase"] != "idle" {
		t.Fatalf("data = %s", out.data)
	}
}

// 公开入口 Call 走的是 PipeName()（这里用测试专用覆盖名）。
func TestCallUsesPipeNameOverride(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	t.Setenv(JSONPipeNameEnv, stub.name)

	type res struct {
		data json.RawMessage
		err  error
	}
	ch := make(chan res, 1)
	go func() {
		data, err := NewClient().Call("status", nil)
		ch <- res{data, err}
	}()
	conn := stub.accept(t)
	request := handshake(t, conn, key, stubNonce)
	if cmd, args, keys := parseRequest(t, request); cmd != "status" || args != "{}" || keys != 2 {
		t.Fatalf("请求行 = %q（cmd=%q args=%s 顶层键=%d）", request, cmd, args, keys)
	}
	if err := conn.send(map[string]any{"ok": true, "data": map[string]any{"pid": 7}}); err != nil {
		t.Fatalf("回结果: %v", err)
	}
	out := <-ch
	if out.err != nil {
		t.Fatalf("Call: %v", out.err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.data, &got); err != nil || got["pid"] != float64(7) {
		t.Fatalf("data = %s（err=%v）", out.data, err)
	}
}

// Probe 走公开入口（PipeName 覆盖），一问一答拿到 status 数据。
func TestProbeRoundTrip(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	t.Setenv(JSONPipeNameEnv, stub.name)

	result := make(chan callOutcome, 1)
	go func() {
		data, err := NewClient().Probe(3 * time.Second)
		result <- callOutcome{data: data, err: err}
	}()
	conn := stub.accept(t)
	request := handshake(t, conn, key, stubNonce)
	if cmd, args, keys := parseRequest(t, request); cmd != "status" || args != "{}" || keys != 2 {
		t.Fatalf("Probe 的请求行 = %q（cmd=%q args=%s 顶层键=%d）", request, cmd, args, keys)
	}
	if err := conn.send(map[string]any{"ok": true, "data": map[string]any{"phase": "idle", "pid": 9}}); err != nil {
		t.Fatalf("回结果: %v", err)
	}
	out := <-result
	if out.err != nil {
		t.Fatalf("Probe: %v", out.err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.data, &got); err != nil {
		t.Fatalf("data 不是 JSON：%v（%s）", err, out.data)
	}
	if got["phase"] != "idle" || got["pid"] != float64(9) {
		t.Fatalf("data = %s", out.data)
	}
}

// Probe 的时限必须生效：装死的服务端到点就回，不能拖到命令表里的 8 秒。
func TestProbeHonorsTimeout(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	t.Setenv(JSONPipeNameEnv, stub.name)
	const timeout = 500 * time.Millisecond

	result := make(chan callOutcome, 1)
	go func() {
		start := time.Now()
		data, err := NewClient().Probe(timeout)
		result <- callOutcome{data: data, err: err, elapsed: time.Since(start)}
	}()
	conn := stub.accept(t)
	handshake(t, conn, key, stubNonce) // 吃到请求，然后装死
	select {
	case out := <-result:
		assertBridgeError(t, out.err, stub.name, "没完成")
		if out.elapsed < timeout/2 {
			t.Fatalf("不到 %v 就回来了：%v", timeout, out.elapsed)
		}
		if out.elapsed > 5*time.Second {
			t.Fatalf("Probe 没用自己的时限：用了 %v", out.elapsed)
		}
	case <-time.After(4 * time.Second):
		t.Fatal("Probe 的超时没生效（时限没传下去？）")
	}
}

// 装死的服务端：超时必须生效且带管道名。把超时值改大这条就红。
func TestCallTimesOutOnSilentServer(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	const timeout = 500 * time.Millisecond
	result := callAsync(stub.name, "status", nil, timeout)
	conn := stub.accept(t)
	handshake(t, conn, key, stubNonce) // 吃到请求，然后装死
	select {
	case out := <-result:
		assertBridgeError(t, out.err, stub.name, "没完成")
		if out.elapsed < timeout/2 {
			t.Fatalf("不到 %v 就回来了：%v", timeout, out.elapsed)
		}
	case <-time.After(4 * time.Second):
		t.Fatal("超时没生效（超时值被改大了？）")
	}
}

// {"ok": false} 必须变成错误；把 ok:false 当成功这条就红。
func TestCallRejectsOKFalse(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status", nil, 3*time.Second)
	conn := stub.accept(t)
	handshake(t, conn, key, stubNonce)
	if err := conn.send(map[string]any{"ok": false, "error": "引擎在忙。"}); err != nil {
		t.Fatalf("回结果: %v", err)
	}
	out := <-result
	assertBridgeError(t, out.err, stub.name, "引擎在忙。")
}

// 回的不是 JSON：错误文本要带管道名。
func TestCallRejectsNonJSONReply(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status", nil, 3*time.Second)
	conn := stub.accept(t)
	handshake(t, conn, key, stubNonce)
	if err := conn.write([]byte("not json\n")); err != nil {
		t.Fatalf("回非 JSON: %v", err)
	}
	out := <-result
	assertBridgeError(t, out.err, stub.name, "不是 JSON")
}

// nonce 是空的：必须当握手失败。nonce 校验恒真这条就红。
func TestCallRejectsEmptyNonce(t *testing.T) {
	useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status", nil, 3*time.Second)
	conn := stub.accept(t)
	if err := conn.send(map[string]any{"nonce": ""}); err != nil {
		t.Fatalf("发 nonce: %v", err)
	}
	// 正确的客户端立刻收手；变异体（不校验 nonce）会继续发 auth+请求，
	// 这里就回一个 ok:true，让变异体「成功」返回。
	if _, err := conn.readLine(); err == nil {
		_ = conn.send(map[string]any{"ok": true, "data": map[string]any{"pid": 1}})
	}
	out := <-result
	assertBridgeError(t, out.err, stub.name, "nonce 是空的")
}

// nonce 不是 JSON：同样必须当握手失败。
func TestCallRejectsNonJSONNonce(t *testing.T) {
	useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status", nil, 3*time.Second)
	conn := stub.accept(t)
	if err := conn.write([]byte("garbage\n")); err != nil {
		t.Fatalf("发垃圾 nonce: %v", err)
	}
	if _, err := conn.readLine(); err == nil {
		_ = conn.send(map[string]any{"ok": true, "data": map[string]any{"pid": 1}})
	}
	out := <-result
	assertBridgeError(t, out.err, stub.name, "nonce 不是 JSON")
}

// 合法的 JSON、但一行超过 64 KiB：长度闸必须拒绝。去掉长度闸这条就红。
func TestCallRejectsOverlongReply(t *testing.T) {
	key := useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status", nil, 3*time.Second)
	conn := stub.accept(t)
	handshake(t, conn, key, stubNonce)
	big, err := json.Marshal(map[string]any{
		"ok":   true,
		"data": map[string]any{"pad": strings.Repeat("a", MaxRequestBytes)},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := conn.write(append(big, '\n')); err != nil {
		t.Fatalf("回大行: %v", err)
	}
	out := <-result
	assertBridgeError(t, out.err, stub.name, "64 KiB")
}

// 发出的请求超过 64 KiB：连接都不该写出去，直接报错。
func TestCallRejectsOverlongRequest(t *testing.T) {
	useTempKey(t)
	stub := newStubPipe(t)
	result := callAsync(stub.name, "status",
		map[string]any{"blob": strings.Repeat("a", MaxRequestBytes+1)}, 3*time.Second)
	conn := stub.accept(t)
	if err := conn.send(map[string]any{"nonce": stubNonce}); err != nil {
		t.Fatalf("发 nonce: %v", err)
	}
	// 客户端在写请求之前就该拒绝；读到 EOF 是预期的。
	if _, err := conn.readLine(); err != nil && !errors.Is(err, io.EOF) {
		t.Fatalf("读 auth: %v", err)
	}
	out := <-result
	assertBridgeError(t, out.err, stub.name, "请求超过")
}

// data 缺失 / 是 null：归一成 {}（有意选择，见偏差登记）。
func TestCallNormalizesMissingData(t *testing.T) {
	key := useTempKey(t)
	replies := []map[string]any{
		{"ok": true},
		{"ok": true, "data": nil},
	}
	for _, reply := range replies {
		stub := newStubPipe(t)
		result := callAsync(stub.name, "status", nil, 3*time.Second)
		conn := stub.accept(t)
		handshake(t, conn, key, stubNonce)
		if err := conn.send(reply); err != nil {
			t.Fatalf("回结果: %v", err)
		}
		out := <-result
		if out.err != nil {
			t.Fatalf("%v: %v", reply, out.err)
		}
		if string(out.data) != "{}" {
			t.Fatalf("%v: data = %s，想要 {}", reply, out.data)
		}
	}
}

// 未知命令：连接之前就拒绝（走公开入口，查的是命令表）。
func TestCallRejectsUnknownCommand(t *testing.T) {
	_, err := NewClient().Call("nope", nil)
	if err == nil {
		t.Fatal("未知命令应当被拒")
	}
	if !strings.Contains(err.Error(), "未知命令") {
		t.Fatalf("错误文本 = %v，想要含「未知命令」", err)
	}
}
