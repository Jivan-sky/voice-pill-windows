package bridge

import (
	"bufio"
	"strings"
	"testing"
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
