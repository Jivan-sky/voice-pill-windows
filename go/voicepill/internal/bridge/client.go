// Package bridge 是 Voice Pill 控制面 NDJSON 通道的客户端。
//
// 与 Python 侧 src/bridge.py 的 JsonBridgeServer 对齐：一次连接一条请求，
// UTF-8、紧凑 JSON、以换行分帧。协议见
// docs/superpowers/specs/2026-10-03-插件换Go单exe.md 第 6 节。
package bridge

import (
	"bufio"
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	// JSONPipePrefix 与 Python 侧 bridge.JSON_PIPE_PREFIX 逐字一致。
	JSONPipePrefix = `\\.\pipe\VoicePill-Json-`
	// JSONPipeNameEnv 整个覆盖管道名（自测用），对齐 Python 侧的
	// VOICEPILL_JSON_PIPE_NAME。
	JSONPipeNameEnv = "VOICEPILL_JSON_PIPE_NAME"
	// KeyFileName 是共享密钥在 %LOCALAPPDATA%\VoicePill 下的文件名。
	KeyFileName = "bridge.key"
	// MaxRequestBytes 是单行上限，对齐 Python 侧 MAX_REQUEST_BYTES。
	MaxRequestBytes = 64 * 1024
)

// Commands 是 NDJSON 通道的八个命令，与 Python 侧 bridge.COMMANDS 一致。
var Commands = []string{"status", "start", "stop", "cancel", "take", "speak", "shutup", "capture"}

// commandTimeouts 是各命令「连接 + 握手 + 一问一答」的整体上限。已有七条取自
// Python 侧 plugins\voice-pill\mcp_server.py 里 _call 的实际取值；capture 不在
// 那份表里（它不是 MCP 工具，只走通道），按它实际干的事取 15 秒——它要往
// 用户的库目录写一个文件，而那个目录可能在同步盘上，给足余量比抢时间对。
var commandTimeouts = map[string]time.Duration{
	"status":  8 * time.Second,
	"start":   15 * time.Second,
	"stop":    15 * time.Second,
	"cancel":  15 * time.Second,
	"take":    8 * time.Second,
	"speak":   10 * time.Second,
	"shutup":  8 * time.Second,
	"capture": 15 * time.Second,
}

var errLineTooLong = errors.New("这一行超过 64 KiB 上限")

// BridgeError 是控制面不可用或对端返回了错误。文本直接给模型看。
type BridgeError struct{ msg string }

func (e *BridgeError) Error() string { return e.msg }

func newBridgeError(format string, args ...any) *BridgeError {
	return &BridgeError{msg: fmt.Sprintf(format, args...)}
}

// AppDir 返回引擎的控制面目录：%LOCALAPPDATA%\VoicePill。
//
// Python 侧用的是 config.app_dir()。引擎根的自解析（含插件目录联接）属于
// 任务 7，这里先用环境变量推出同一个位置。
func AppDir() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), "VoicePill")
}

// KeyPath 是共享密钥文件的路径。
func KeyPath() string { return filepath.Join(AppDir(), KeyFileName) }

// normcase 复刻 Python os.path.normcase 在 Windows 上的行为：转小写、
// 正斜杠换成反斜杠（不做 Clean）。
func normcase(path string) string {
	return strings.ReplaceAll(strings.ToLower(path), "/", `\`)
}

// PipeNameFor 返回某个引擎目录对应的 NDJSON 管道名。
func PipeNameFor(engineDir string) string {
	sum := sha256.Sum256([]byte(normcase(engineDir)))
	return JSONPipePrefix + hex.EncodeToString(sum[:])[:16]
}

// PipeName 是当前进程要连的管道名；VOICEPILL_JSON_PIPE_NAME 非空时整个覆盖。
func PipeName() string {
	if override := strings.TrimSpace(os.Getenv(JSONPipeNameEnv)); override != "" {
		return override
	}
	return PipeNameFor(AppDir())
}

// LoadOrCreateKey 读写共享密钥（trim 后 64 位 hex = 32 字节）。
//
// 不存在就自己造：32 字节随机转 64 位 hex，用 O_CREATE|O_EXCL 落盘，
// 撞车就重读——与 Python 侧「先起的造，后起的读」一致。
func LoadOrCreateKey() ([]byte, error) {
	path := KeyPath()
	if key, err := readKey(path); err == nil {
		return key, nil
	}

	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, newBridgeError("生成控制面密钥失败：%v", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, newBridgeError("创建控制面目录 %s 失败：%v", filepath.Dir(path), err)
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		if os.IsExist(err) {
			if key, rerr := readKey(path); rerr == nil {
				return key, nil
			}
		}
		return nil, newBridgeError("写控制面密钥 %s 失败：%v", path, err)
	}
	if _, err := f.WriteString(hex.EncodeToString(key)); err != nil {
		f.Close()
		return nil, newBridgeError("写控制面密钥 %s 失败：%v", path, err)
	}
	if err := f.Close(); err != nil {
		return nil, newBridgeError("写控制面密钥 %s 失败：%v", path, err)
	}
	return key, nil
}

func readKey(path string) ([]byte, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	text := strings.TrimSpace(string(raw))
	if len(text) != 64 {
		return nil, fmt.Errorf("密钥文件 %s 不是 64 位十六进制", path)
	}
	key, err := hex.DecodeString(text)
	if err != nil || len(key) != 32 {
		return nil, fmt.Errorf("密钥文件 %s 不是 64 位十六进制", path)
	}
	return key, nil
}

// AuthToken 是挑战应答的应答：HMAC-SHA256(key, ASCII(nonce))，64 位小写 hex。
func AuthToken(key []byte, nonce string) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(nonce))
	return hex.EncodeToString(mac.Sum(nil))
}

// Client 是 NDJSON 通道的客户端。无状态：每次 Call 新建一条连接。
type Client struct{}

// NewClient 造一个客户端。
func NewClient() *Client { return &Client{} }

// Call 发一条命令，返回响应里的 data。
func (c *Client) Call(cmd string, args map[string]any) (json.RawMessage, error) {
	timeout, ok := commandTimeouts[cmd]
	if !ok {
		return nil, newBridgeError("未知命令：%s", cmd)
	}
	if args == nil {
		args = map[string]any{}
	}
	return call(PipeName(), cmd, args, timeout)
}

// Probe 用调用方给的时限探一次 status：连不上 / 超时 / 对端报错都返回错误。
//
// 对齐 Python 侧 src/bridge.py 的 probe()：那里连不上返回 None，Go 里用非 nil
// 错误表达同一件事，调用方据此判断「引擎没在跑」。internal/tools 的
// voice_pill_status 与 _ensure_engine 的轮询都走它。
func (c *Client) Probe(timeout time.Duration) (json.RawMessage, error) {
	return call(PipeName(), "status", nil, timeout)
}

// call 把「连接 + 握手 + 一问一答」整体放进一个 goroutine，超时就把连接
// 关掉（Windows 上关句柄会打断阻塞的 ReadFile），返回人话错误。
func call(pipe string, cmd string, args map[string]any, timeout time.Duration) (json.RawMessage, error) {
	if args == nil {
		args = map[string]any{}
	}
	key, err := LoadOrCreateKey()
	if err != nil {
		return nil, err
	}

	type outcome struct {
		data json.RawMessage
		err  error
	}
	done := make(chan outcome, 1)

	var (
		mu      sync.Mutex
		conn    *os.File
		expired bool
	)

	go func() {
		f, err := os.OpenFile(pipe, os.O_RDWR, 0)
		if err != nil {
			done <- outcome{nil, newBridgeError(
				"连不上 Voice Pill 控制面（%s，常驻引擎没在跑？）：%v", pipe, err)}
			return
		}
		mu.Lock()
		if expired {
			mu.Unlock()
			f.Close()
			return
		}
		conn = f
		mu.Unlock()
		defer f.Close()

		data, err := exchange(f, pipe, key, cmd, args)
		done <- outcome{data, err}
	}()

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case out := <-done:
		return out.data, out.err
	case <-timer.C:
		mu.Lock()
		expired = true
		if conn != nil {
			conn.Close()
		}
		mu.Unlock()
		return nil, newBridgeError("控制面 %.1f 秒没完成（%s）。", timeout.Seconds(), pipe)
	}
}

// exchange 走完 NDJSON 通道的握手与一问一答。
//
// 帧序对齐 Python 侧 JsonBridgeServer._serve_connection：服务端先发 nonce，
// 客户端回 auth，紧接着发请求行；服务端只在鉴权失败时回一行 ok:false，
// 鉴权通过就直接用最终响应作答（没有中间 ack）。
func exchange(f *os.File, pipe string, key []byte, cmd string, args map[string]any) (json.RawMessage, error) {
	reader := bufio.NewReaderSize(f, 4096)

	nonceLine, err := readLine(reader)
	if err != nil {
		return nil, newBridgeError("控制面 %s 握手失败（没收到 nonce）：%v", pipe, err)
	}
	var challenge struct {
		Nonce string `json:"nonce"`
	}
	if err := json.Unmarshal(nonceLine, &challenge); err != nil {
		return nil, newBridgeError("控制面 %s 握手失败（nonce 不是 JSON 对象）：%v", pipe, err)
	}
	if challenge.Nonce == "" {
		return nil, newBridgeError("控制面 %s 握手失败（nonce 是空的）。", pipe)
	}

	authLine, err := marshalLine(map[string]any{"auth": AuthToken(key, challenge.Nonce)})
	if err != nil {
		return nil, newBridgeError("控制面 %s：%v", pipe, err)
	}
	requestLine, err := marshalLine(map[string]any{"cmd": cmd, "args": args})
	if err != nil {
		return nil, newBridgeError("控制面 %s：%v", pipe, err)
	}
	if _, err := f.Write(append(authLine, requestLine...)); err != nil {
		return nil, newBridgeError("控制面 %s 通信中断：%v", pipe, err)
	}

	replyLine, err := readLine(reader)
	if err != nil {
		return nil, newBridgeError("控制面 %s 通信中断：%v", pipe, err)
	}
	var reply struct {
		OK    bool            `json:"ok"`
		Error string          `json:"error"`
		Data  json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(replyLine, &reply); err != nil {
		return nil, newBridgeError("控制面 %s 回的不是 JSON 对象：%v", pipe, err)
	}
	if !reply.OK {
		if reply.Error != "" {
			return nil, newBridgeError("控制面 %s 拒绝了 %s：%s", pipe, cmd, reply.Error)
		}
		return nil, newBridgeError("控制面 %s 拒绝了 %s。", pipe, cmd)
	}
	if len(reply.Data) == 0 || string(reply.Data) == "null" {
		return json.RawMessage("{}"), nil
	}
	return reply.Data, nil
}

func marshalLine(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, newBridgeError("序列化请求失败：%v", err)
	}
	line := make([]byte, 0, len(raw)+1)
	line = append(line, raw...)
	line = append(line, '\n')
	if len(line) > MaxRequestBytes {
		return nil, newBridgeError("请求超过 %d 字节上限", MaxRequestBytes)
	}
	return line, nil
}

// readLine 读到换行符为止；超过 MaxRequestBytes 就报错，不无限读。
func readLine(r *bufio.Reader) ([]byte, error) {
	buf := make([]byte, 0, 256)
	for {
		chunk, err := r.ReadSlice('\n')
		buf = append(buf, chunk...)
		if err == nil {
			line := bytes.TrimSuffix(buf[:len(buf)-1], []byte("\r"))
			if len(line) > MaxRequestBytes {
				return nil, errLineTooLong
			}
			return line, nil
		}
		if len(buf) > MaxRequestBytes {
			return nil, errLineTooLong
		}
		if errors.Is(err, bufio.ErrBufferFull) {
			continue
		}
		return nil, err
	}
}
