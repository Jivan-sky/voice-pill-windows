package sentinel

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

// 藏起「Windows 替我们开的那个控制台窗口」。
//
// 为什么需要
// ----------
// exe 是 WINDOWS_CUI 子系统（bin/voicepill.exe 的 PE 头 Subsystem=3）。从
// HKCU\...\Run 拉起时，Windows 会给它**开一个自己的控制台窗口**，而哨兵是
// 常驻的——那个黑窗口会一直杵在桌面上，每次登录都来一个。
//
// 为什么不干脆把 PE 头改成 GUI 子系统
// ----------------------------------
// 能根治，但会改掉 exe 对**所有**调用方的行为，代价最大的一处是三个钩子：
// 钩子是被 hook*.cmd 拉起来的，而 cmd.exe 对 GUI 子系统程序**不等待**——.cmd
// 会立刻返回，宿主读到的 stdout 是空的，钩子的 {"continue":true} 就丢了。
// 为了一个登录窗口去动这条已经在工作的路，不值。
//
// 判据：只藏「独占的那个」控制台
// -----------------------------
// 必须把两种控制台分开，否则会把用户的终端一起藏掉：
//   - 从 Run 键拉起 → Windows 新建的，挂在上头的**只有我们自己** → 藏。
//   - 用户在终端里敲 voicepill.exe sentinel → 和 shell 共享 → 不许碰。
//
// 靠 GetConsoleProcessList 数挂在上头的进程数，只有 1 个才动手。
//
// 代价：窗口是先建后藏，登录时会闪一下（毫秒级）。要彻底不闪只能改 GUI
// 子系统，见上。
var (
	procGetConsoleWindow      = windows.NewLazySystemDLL("kernel32.dll").NewProc("GetConsoleWindow")
	procGetConsoleProcessList = windows.NewLazySystemDLL("kernel32.dll").NewProc("GetConsoleProcessList")
	procShowWindow            = windows.NewLazySystemDLL("user32.dll").NewProc("ShowWindow")
)

// SW_HIDE：ShowWindow 的第二个参数。
const swHide = 0

// 三个口都做成变量，单测换掉它们就不碰真的窗口（与本包其余口同一个纪律）。
var (
	consoleWindow  = getConsoleWindow
	consoleCount   = getConsoleProcessCount
	hideWindowFunc = hideWindow
)

func getConsoleWindow() uintptr {
	hwnd, _, _ := procGetConsoleWindow.Call()
	return hwnd
}

// getConsoleProcessCount 数挂在本进程控制台上的进程数；没有控制台时返回 0。
// 缓冲区故意开得比真实数量大：函数在装不下时会回「需要多大」，那种情况下
// 我们只关心「是不是 1」，64 已经远超任何正常情形。
func getConsoleProcessCount() int {
	buf := make([]uint32, 64)
	n, _, _ := procGetConsoleProcessList.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	return int(n)
}

func hideWindow(hwnd uintptr) bool {
	// ShowWindow 返回的是**改动之前**的可见状态：非 0 = 原来可见、现在藏了。
	prev, _, _ := procShowWindow.Call(hwnd, swHide)
	return prev != 0
}

// HideOwnConsole 藏掉「Windows 替我们开的」那个控制台窗口。
// 返回 (藏没藏成, 原因)，原因给日志用：
//
//	hidden      藏了
//	no_console  本来就没有控制台（宿主用 CREATE_NO_WINDOW 拉起，或 stdout 是管道）
//	shared      控制台是共用的（用户的终端）——不动
//	show_failed 有独占的控制台，但 ShowWindow 说它本来就不显示
func HideOwnConsole() (bool, string) {
	hwnd := consoleWindow()
	if hwnd == 0 {
		return false, "no_console"
	}
	if consoleCount() > 1 {
		return false, "shared"
	}
	if !hideWindowFunc(hwnd) {
		return false, "show_failed"
	}
	return true, "hidden"
}
