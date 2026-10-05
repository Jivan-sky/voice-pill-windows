package engine

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// 停用纸条的判据是**两张纸的先后**，不是 pid 相等。这里把 app_dir 指到临时目录。
func TestStoppedByUser(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("LOCALAPPDATA", dir)
	app := filepath.Join(dir, "VoicePill")
	if err := os.MkdirAll(app, 0o777); err != nil {
		t.Fatal(err)
	}
	anchor := filepath.Join(app, "anchor.json")
	stop := filepath.Join(app, "stopped.json")

	writeAnchor := func(pid int, at float64) {
		body := fmt.Sprintf("{\"pid\":%d,\"image\":\"C:\\\\x\\\\ChatGPT.exe\",\"at\":%v}", pid, at)
		if err := os.WriteFile(anchor, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	writeStop := func(pid int, at float64) {
		body := fmt.Sprintf("{\"pid\":%d,\"at\":%v}", pid, at)
		if err := os.WriteFile(stop, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	// 两张纸都没有：不许拦路（能力比绑生命周期重要）。
	if got, pid := StoppedByUser(); got || pid != 0 {
		t.Errorf("没有纸条时应当放行，得到 (%v, %d)", got, pid)
	}

	// 只有锚点：没人停过。
	writeAnchor(42924, 1000)
	if got, _ := StoppedByUser(); got {
		t.Error("只有锚点（没有停用纸）时应当放行")
	}

	// 锚点在先、停用在后：这一代被停过 → 拦。
	writeStop(42924, 1001)
	if got, pid := StoppedByUser(); !got || pid != 42924 {
		t.Errorf("停用纸更新时应当拦住，得到 (%v, %d)", got, pid)
	}

	// 锚点被新一代 Codex 重写（更晚）：新的一代，放行。
	writeAnchor(99999, 1002)
	if got, pid := StoppedByUser(); got || pid != 42924 {
		t.Errorf("锚点更新（换了一代）时应当放行，得到 (%v, %d)", got, pid)
	}

	// 锚点被删（钩子还没写过）：不拦。
	if err := os.Remove(anchor); err != nil {
		t.Fatal(err)
	}
	if got, _ := StoppedByUser(); got {
		t.Error("没有锚点时不应当拦")
	}

	// 停用纸条格式坏掉：当没有这张纸。
	if err := os.WriteFile(stop, []byte("not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, _ := StoppedByUser(); got {
		t.Error("停用纸条坏掉时不应当拦")
	}
}
