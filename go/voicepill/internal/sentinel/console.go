package sentinel

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

// 藏起「Windows 替我们开的那个控制台窗口」。
//
// 为什么需要
// ----------
// exe 曾经是 WINDOWS_CUI 子系统（PE 头 Subsystem=3）。从
// HKCU\...\Run 拉起时，Windows 会给它**开一个自己的控制台窗口**，而哨兵是
// 常驻的——那个黑窗口会一直杵在桌面上，每次登录都来一个。
//
// 后来还是把 PE 头改成了 GUI 子系统（2026-10-06）
// ----------------------------------
// 原来这里的结论是"不改"，理由是"cmd.exe 对 GUI 子系统程序**不等待**——.cmd 会
// 立刻返回，宿主读到的 stdout 是空的，钩子的 {"continue":true} 就丢了"。实测把这条
// 推翻了。照钩子那条链复刻量了一遍（cmd /c hook.cmd + 全管道 + CREATE_NO_WINDOW，
// cui / gui 两份同源码构建，被测程序自己睡 2 秒）：
//
//	cmd /c hook-cui.cmd   elapsed=2.08s  exit=7  stdout='probe-stdout-ok'  stdin 照收
//	cmd /c hook-gui.cmd   elapsed=2.09s  exit=7  stdout='probe-stdout-ok'  stdin 照收
//
// 也就是 cmd **会等**，stdout / stdin / 退出码三样都通（钩子的 JSON 载荷走 stdin）。
// 另外 80 项 exe 自测（tools/pluginexe-selftest.py）对 GUI 构建全过，用户在终端里
// 直敲 `voicepill.exe sentinel --status` 也照常出字、退出码正常。
//
// 所以判据换成更硬的一句：**别让 Windows 建这个控制台**。GUI 子系统下
// GetConsoleWindow() 恒为 0，连"先建后藏"那一闪也没了。构建带 `-H windowsgui`，
// 见 tools/build-plugin-exe.ps1。
//
// 判据：只藏「独占的那个」控制台
// -----------------------------
// 必须把两种控制台分开，否则会把用户的终端一起藏掉：
//   - 从 Run 键拉起 → Windows 新建的，挂在上头的**只有我们自己** → 藏。
//   - 用户在终端里敲 voicepill.exe sentinel → 和 shell 共享 → 不许碰。
//
// 靠 GetConsoleProcessList 数挂在上头的进程数，只有 1 个才动手。
//
// 代价：窗口是先建后藏，登录时会闪一下（毫秒级）。GUI 子系统构建下这条路根本
// 走不到（没有控制台可藏，回的是 "no_console"）——留着它是给"有人不带
// -H windowsgui 编了一次"的 CUI 构建兜底，判据仍然成立，见上。
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
