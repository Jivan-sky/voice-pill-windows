# -*- coding: utf-8 -*-
"""为什么收不到键盘？——窗口站 / 桌面 + 钩子自激自收 两项判定。

背景
----
`tools/shell-selftest.py` 在**前台**和**后台**各跑过一遍，都是 0 条原始按键
事件（钩子装得上，`hook_installed=True`）。有两种可能，必须分开：

  A) 进程不在交互桌面上（`WinSta0\\Default`）。
     不在的话，`WH_KEYBOARD_LL` 照样能装、照样返回成功，但**永远收不到
     别的进程的物理按键**——这是"静默成功"，最坑的那种。
     由 Claude Code / 计划任务 / 服务拉起来的进程常见这个下场。

  B) 进程在交互桌面上，钩子也正常，只是那 45 秒里确实没人按键。

这个探针一次把两者分开：

  判定 1  本进程的窗口站名 + 桌面名（与 `WinSta0` / `Default` 比对）
  判定 2  **合成按键自激自收**：自己 `SendInput` 发一个键，看自己的低级钩子
          收不收得到。收到 = 钩子 + 消息泵这条链是通的（排除"泵没转"）。
          注意：合成事件带 `LLKHF_INJECTED`，本探针**不**过滤它
          （`hotkey.py` 里过滤是为了防自家粘贴自激，这里正相反，要它进来）。

两者合起来读：
  · 桌面 ≠ Default           → 问题在 A，得换启动方式
  · 桌面 = Default 且自激收到 → 链路全通，那 0 条就是 B（没人按键）
  · 桌面 = Default 但自激收不到 → 钩子/消息泵有问题，另查

用法：
    python tools/desktop-probe.py
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM,
                              wintypes.LPARAM)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_QUIT = 0x0012
UOI_NAME = 2
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_PROBE = 0x77           # F8，随便挑一个没人占的


# ---------- 判定 1：窗口站 / 桌面 ----------

def _obj_name(handle) -> str:
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    ok = user32.GetUserObjectInformationW(
        handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed))
    return buf.value if ok else "<取不到 err=%d>" % ctypes.get_last_error()


_RID_NAME = {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
             0x2100: "Medium-Plus", 0x3000: "High", 0x4000: "System"}


def _integrity() -> str:
    """进程完整性级别。低于 Medium 的话 SendInput 会被 UIPI 挡。

    被 Claude Code 的沙箱拉起来的进程有可能落在 AppContainer / Low 里，
    那就解释得通"钩子装得上、却收不到也发不出"。
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    TOKEN_QUERY, TokenIntegrityLevel = 0x0008, 25
    # 伪句柄是 64 位 0xFFFF...FF。不声明 restype，ctypes 按 c_int 收成
    # 0xFFFFFFFF，传给 OpenProcessToken 就变 err=6 ERROR_INVALID_HANDLE。
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    # 这两个收 PSID。不声明 argtypes 的话 ctypes 按 c_int 塞，指针高位被截，
    # 报 OverflowError: int too long to convert。
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    tok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(tok)):
        return "<OpenProcessToken 失败 err=%d>" % ctypes.get_last_error()
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(tok, TokenIntegrityLevel, None, 0,
                                     ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(tok, TokenIntegrityLevel, buf,
                                            size.value, ctypes.byref(size)):
            return "<GetTokenInformation 失败 err=%d>" % ctypes.get_last_error()
        # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attr; } }
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
        cnt = advapi32.GetSidSubAuthorityCount(psid).contents.value
        rid = advapi32.GetSidSubAuthority(psid, cnt - 1).contents.value
        return "0x%04X %s" % (rid, _RID_NAME.get(rid, "未知"))
    finally:
        kernel32.CloseHandle(tok)


def report_desktop() -> None:
    print("[判定 1] 本进程在哪个窗口站 / 桌面")
    station = user32.GetProcessWindowStation()
    desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    sname = _obj_name(station)
    dname = _obj_name(desk)
    print("  窗口站：%s" % sname)
    print("  桌面  ：%s" % dname)

    sid = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                  ctypes.byref(sid))
    print("  会话号：%d（交互登录一般是 1）" % sid.value)
    print("  完整性级别：%s" % _integrity())

    fg = user32.GetForegroundWindow()
    print("  当前前台窗口句柄：0x%X" % fg)
    if fg:
        tid = user32.GetWindowThreadProcessId(fg, None)
        fdesk = user32.GetThreadDesktop(tid)
        print("  前台窗口所在桌面：%s" % _obj_name(fdesk))

    good = (sname.upper() == "WINSTA0" and dname.lower() == "default")
    print("  → %s" % ("✅ 在交互桌面上，钩子有资格收到物理按键"
                      if good else
                      "❌ **不在交互桌面上**。钩子能装、但物理按键永远收不到。"))
    print()


# ---------- 判定 2：合成按键自激自收 ----------

class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    # 联合体必须按**最大**成员留位：x64 上 MOUSEINPUT 是 32 字节
    # （4+4+4+4+4+8，尾部对齐），KEYBDINPUT 只有 24。
    # 留成 24 会让 sizeof(INPUT)=32，而 SendInput 要求 cbSize 必须等于
    # 系统眼里的 sizeof(INPUT)=40，否则直接 err=87 ERROR_INVALID_PARAMETER，
    # 一条都不发 —— 这个坑上面判定 2 第一次就踩了。
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT),
                    ("padding", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


received = []


def report_synthetic() -> None:
    print("[判定 2] 自己发一个合成按键，看自己的钩子收不收得到")
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                                 ctypes.c_int]

    state = {"installed": False, "err": 0, "tid": 0, "filtered": 0}
    ready = threading.Event()

    def on_hook(nCode, wParam, lParam):
        if nCode == 0 and wParam in (WM_KEYDOWN, WM_KEYUP):
            kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            state["filtered"] += 1        # 这里**不过滤** INJECTED，故意的
            received.append((wParam == WM_KEYUP, int(kb.vkCode),
                             int(kb.scanCode), int(kb.flags)))
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    proc_ref = HOOKPROC(on_hook)

    def hook_loop():
        state["tid"] = int(kernel32.GetCurrentThreadId())
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc_ref,
                                        kernel32.GetModuleHandleW(None), 0)
        if not hook:
            state["err"] = ctypes.get_last_error()
            ready.set()
            return
        state["installed"] = True
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(hook)

    t = threading.Thread(target=hook_loop, daemon=True)
    t.start()
    ready.wait(3.0)

    if not state["installed"]:
        print("  ❌ 钩子都没装上，err=%d" % state["err"])
        return
    print("  钩子已装上，消息泵线程 id=%d" % state["tid"])

    # 发一轮按下 + 抬起
    def send(flags: int) -> int:
        inp = _INPUT(type=INPUT_KEYBOARD,
                     ki=_KEYBDINPUT(wVk=VK_PROBE, wScan=0, dwFlags=flags,
                                    time=0, dwExtraInfo=None))
        ctypes.set_last_error(0)
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if n == 0:
            # 0 就是一条都没发出去，不是"钩子没收到"。必须看 errno，
            # 常见 5 = ERROR_ACCESS_DENIED（被 UIPI / 沙箱令牌挡住）。
            print("     SendInput 被拒：err=%d" % ctypes.get_last_error())
        return n

    n1 = send(0)
    time.sleep(0.08)
    n2 = send(KEYEVENTF_KEYUP)
    time.sleep(0.5)                # 给钩子线程把队列消化掉

    if state["tid"]:
        user32.PostThreadMessageW(state["tid"], WM_QUIT, 0, 0)

    print("  SendInput 返回：按下 %d 条 / 抬起 %d 条" % (n1, n2))
    print("  钩子收到：%d 条" % len(received))
    for up, vk, scan, flags in received:
        print("     · %s vk=0x%02X scan=0x%02X flags=0x%02X%s"
              % ("[松开]" if up else "[按下]", vk, scan, flags,
                 "  (INJECTED)" if flags & 0x10 else ""))
    if received:
        print("  → ✅ 钩子 + 消息泵这条链是通的")
    else:
        print("  → ❌ 自发自收都收不到，钩子链路本身有问题")
    print()


if __name__ == "__main__":
    print("=" * 68)
    print("键盘可见性判定 · Claude Code 拉起的进程")
    print("=" * 68)
    report_desktop()
    report_synthetic()
    print("=" * 68)
