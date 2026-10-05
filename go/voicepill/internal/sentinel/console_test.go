package sentinel

import "testing"

// 只测判据本身：三个口全换成假的，绝不去碰真的控制台窗口。
func TestHideOwnConsole(t *testing.T) {
	oldW, oldC, oldH := consoleWindow, consoleCount, hideWindowFunc
	defer func() { consoleWindow, consoleCount, hideWindowFunc = oldW, oldC, oldH }()

	cases := []struct {
		name     string
		hwnd     uintptr
		count    int
		hideOK   bool
		want     bool
		reason   string
		wantHide bool // 有没有去调 ShowWindow
	}{
		{"没有控制台：什么都不做", 0, 0, false, false, "no_console", false},
		{"共用（用户在终端里敲）：不许碰用户的终端", 0x100, 2, false, false, "shared", false},
		{"独占（Run 键拉起）：藏", 0x100, 1, true, true, "hidden", true},
		{"本来就不显示：如实说没藏成", 0x100, 1, false, false, "show_failed", true},
	}
	for _, c := range cases {
		called := false
		consoleWindow = func() uintptr { return c.hwnd }
		consoleCount = func() int { return c.count }
		hideWindowFunc = func(uintptr) bool { called = true; return c.hideOK }

		got, reason := HideOwnConsole()
		if got != c.want || reason != c.reason {
			t.Errorf("%s: got (%v, %q)，想要 (%v, %q)", c.name, got, reason, c.want, c.reason)
		}
		if called != c.wantHide {
			t.Errorf("%s: 调没调 ShowWindow = %v，想要 %v", c.name, called, c.wantHide)
		}
	}
}
