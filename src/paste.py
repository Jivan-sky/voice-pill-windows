# -*- coding: utf-8 -*-
"""粘贴到光标处：剪贴板 + SendInput(Ctrl+V)。

对齐原版 macOS 的 Sources/PasteController.swift。**语义逐条照搬**——那里每一行
几乎都是踩坑换来的：

  1. 先做剪贴板快照，粘完再还原（用户原来的剪贴板内容不能丢）。
  2. 写入后等一小会儿，让剪贴板服务传播变更。
  3. **等物理修饰键全部松开**再发 Ctrl+V。否则若热键本身带修饰键、用户松手慢，
     合成的按键会带上那些修饰键，目标程序收到 `Ctrl+Alt+V` 之类，**静默失败**。
     原版注释：「Caps Lock 排除在外——它反映的是锁存状态不是按下状态，
     包含它会导致开着大写锁定时每次粘贴都卡住。」
  4. 发键前再校验一次前台窗口没变，变了就放弃。
  5. **粘贴失败时刻意不还原剪贴板**——否则转写结果彻底丢失。宁可留在剪贴板上
     让用户手动 Ctrl+V。
  6. 还原前比对剪贴板序号，用户中途复制了别的东西就不还原。

已知限制：Windows 的 UIPI 会拦截低完整性进程向高完整性窗口注入输入。
若目标程序以管理员身份运行，本工具也需以管理员身份运行才能粘进去。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------- 常量 ----------
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56

# 原版 PasteController.swift:13-26 的三个时延
PASTEBOARD_SETTLE = 0.050      # 写入后 → 发键前
PASTE_COMPLETION = 0.400       # 发键后 → 还原剪贴板前
KEY_EVENT_GAP = 0.010          # Ctrl 按下 → V 按下之间的间隔
MODIFIER_RELEASE_TIMEOUT = 2.0 # 等修饰键松开的上限

# 原版监视的修饰键（**不含 Caps Lock**，理由见模块 docstring）
_MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)


# ---------- SendInput 结构体 ----------
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# ---------- 函数签名（64 位下必须显式声明）----------
user32.GetForegroundWindow.restype = wintypes.HWND
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


class PasteError(Exception):
    pass


# ---------- 前台窗口 ----------

def foreground_window() -> int:
    """当前前台窗口句柄。录制开始时记下，粘贴前再比一次。"""
    return int(user32.GetForegroundWindow() or 0)


# ---------- 剪贴板（纯文本）----------

def _clipboard_open(retries: int = 10) -> bool:
    """剪贴板是独占资源，别的程序占着就打不开。原版有重试的等价物。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> Optional[str]:
    if not _clipboard_open():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """写剪贴板。成功返回 True。"""
    if not _clipboard_open():
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        if text == "":
            return True
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not hmem:
            return False
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            return False
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            kernel32.GlobalUnlock(hmem)
        # 交给系统后不要再 Free —— 所有权已转移
        if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return False
        return True
    finally:
        user32.CloseClipboard()


def clipboard_sequence() -> int:
    """剪贴板序号。还原前比对，防止覆盖用户中途复制的内容。"""
    return int(user32.GetClipboardSequenceNumber())


# ---------- 按键注入 ----------

def _key_event(vk: int, keyup: bool) -> INPUT:
    ev = INPUT()
    ev.type = INPUT_KEYBOARD
    ev.u.ki = KEYBDINPUT(wVk=vk, wScan=0,
                         dwFlags=KEYEVENTF_KEYUP if keyup else 0,
                         time=0, dwExtraInfo=None)
    return ev


def _send(*events: INPUT) -> None:
    arr = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise PasteError("按键注入失败（SendInput 返回 %d/%d）。"
                         "目标程序若以管理员身份运行，本工具也需提权。"
                         % (sent, len(events)))


def wait_for_modifier_release(timeout: float = MODIFIER_RELEASE_TIMEOUT) -> bool:
    """轮询到所有修饰键物理松开。返回是否在超时内达成。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS):
            return True
        time.sleep(0.02)
    return False


def _simulate_ctrl_v() -> None:
    _send(_key_event(VK_CONTROL, False))
    time.sleep(KEY_EVENT_GAP)
    _send(_key_event(VK_V, False))
    time.sleep(KEY_EVENT_GAP)
    _send(_key_event(VK_V, True), _key_event(VK_CONTROL, True))


# ---------- 对外入口 ----------

def paste(text: str, target_hwnd: int,
          on_after: Optional[callable] = None) -> None:
    """把 text 粘到 target_hwnd。

    on_after() 在延迟还原剪贴板时被调用（用于把控制权交还主循环）。

    抛 PasteError 时，**剪贴板刻意保留着 text**（见 docstring 第 5 条），
    调用方应提示用户手动 Ctrl+V。
    """
    if not text:
        return

    original = get_clipboard_text()

    if not set_clipboard_text(text):
        # 写失败：剪贴板已被 EmptyClipboard 清空，尽力把原内容放回去
        if original is not None:
            set_clipboard_text(original)
        raise PasteError("无法写入剪贴板。")

    staged = clipboard_sequence()

    time.sleep(PASTEBOARD_SETTLE)
    wait_for_modifier_release()

    if foreground_window() != target_hwnd:
        raise PasteError("目标窗口已切换，已取消粘贴。文字仍在剪贴板。")

    _simulate_ctrl_v()

    # 延迟还原：不阻塞下一次录音
    def _restore() -> None:
        time.sleep(PASTE_COMPLETION)
        if clipboard_sequence() != staged:
            return          # 用户中途复制了别的东西，不覆盖
        if original is None:
            return
        set_clipboard_text(original)
        if on_after:
            on_after()

    import threading
    threading.Thread(target=_restore, daemon=True).start()
