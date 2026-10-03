# -*- coding: utf-8 -*-
"""全局热键：按住说话，松手结束。

对齐原版 Sources/VoicePill.swift:519-612 的行为：
  * 主键「按住 → 松手」式；**轻点不触发**，长按阈值 180 ms。
  * 备用组合键 `Ctrl+Alt+Space`（对齐原版 macOS 的 `Ctrl+Option+Space`），
    且是**开关式**——按一次开始，再按一次结束。
  * 录音中按 Esc → 取消。
  * **组合键保护**：Fn 会向 OS 发 `E0 63`，所以 Fn+F1 之类会先来一个 Fn 按下。
    长按窗口内出现别的键即撤销，避免误录音。详见 _dispatch 里的注释。

为什么不用 pynput
-----------------
本机主键是 **Fn**，它上报为 `scancode=0x63 / E0 / vkCode=0xFF`（见
docs/移植方案.md 6.2 的实测记录）。`vkCode=0xFF` 是「无虚拟键映射」的意思，
pynput 会不会把它原样透出来是未知数。而裸 `WH_KEYBOARD_LL` 已经被探针
（tools/fn-probe.py）实测证明能稳定拿到 Fn。所以这里直接自己挂钩子，
少一层不确定、少一个依赖。

线程模型（重要）
----------------
钩子回调跑在**装了钩子的那个线程**上，而且 Windows 对它有超时限制
（LowLevelHooksTimeout，默认 300 ms）——回调里做慢活会被系统摘掉钩子。
所以回调只往队列里塞事件，状态机和真正的业务回调都在 worker 线程上跑。
"""
from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import config
from config import KeySpec

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM,
                              wintypes.LPARAM)

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

# 长按阈值：低于此值视为「轻点」，不触发录音（原版 180 ms）
LONG_PRESS_SECONDS = 0.180

# Esc 取消；录音中生效
VK_ESCAPE = 0x1B
ESC = KeySpec(key="esc", label="Esc", vk=VK_ESCAPE, scancode=0x01)

# 备用组合键 Ctrl+Alt+Space（左右 Ctrl / Alt 都认）
VK_SPACE = 0x20
_CTRL_VKS = (0xA2, 0xA3)          # 左/右 Ctrl
_ALT_VKS = (0xA4, 0xA5)           # 左/右 Alt


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _matches(spec: KeySpec, vk: int, scan: int, extended: bool) -> bool:
    """按键是否命中。

    有扫描码的（Fn）**只认扫描码 + E0 标志**：Fn 没有虚拟键映射，
    vkCode 恒为 0xFF，靠 vk 根本区分不开。
    """
    if spec.scancode is not None:
        return scan == spec.scancode and bool(extended) == spec.extended
    return vk == spec.vk


class HotkeyManager:
    """按下 = 开始录音；松开 = 结束。

    回调（都在 worker 线程上触发，不是钩子线程）：
        on_start()       录音开始（长按阈值已过）
        on_stop()        松手，结束录音并出结果
        on_cancel()      录音中按 Esc
        on_quick_tap()   轻点，未达阈值（原版用它做别的，这里只上报）
    """

    def __init__(self,
                 primary: KeySpec,
                 on_start: Optional[Callable[[], None]] = None,
                 on_stop: Optional[Callable[[], None]] = None,
                 on_cancel: Optional[Callable[[], None]] = None,
                 on_quick_tap: Optional[Callable[[], None]] = None,
                 on_raw: Optional[Callable[[bool, int, int, bool], None]] = None,
                 ) -> None:
        self.primary = primary
        self.on_start = on_start or (lambda: None)
        self.on_stop = on_stop or (lambda: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.on_quick_tap = on_quick_tap or (lambda: None)
        # 每个原始按键都会回调一次（up, vk, scan, extended），在 worker 线程上。
        # 只给排查用：零事件时能区分「钩子没收到」和「收到了但没匹配上主键」。
        self.on_raw = on_raw or (lambda _u, _v, _s, _e: None)
        self.raw_count = 0

        self._queue: "queue.Queue" = queue.Queue()
        self.hook_installed = False    # 钩子是否装上了（排查用）
        self.hook_error = 0            # 装不上时的 GetLastError
        self._hook = None
        self._proc_ref = None
        self._hook_thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._thread_id = 0

        self._timer: Optional[threading.Timer] = None
        self._primary_held = False
        self._recording = False
        self._lock = threading.Lock()

        # 备用组合键：Ctrl / Alt 的按下集合 + 是否是「按住」状态
        self._mods_down: set = set()
        self._secondary_held = False

    # ---------- 对外 ----------

    def start(self) -> None:
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True)
        self._hook_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._cancel_timer()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._queue.put(None)          # 让 worker 退出

    @property
    def recording(self) -> bool:
        return self._recording

    def reset(self) -> None:
        """录音结束后复位内部状态，避免下一次录音被旧状态卡住。"""
        with self._lock:
            self._recording = False
            self._primary_held = False
            self._secondary_held = False

    # ---------- 钩子线程 ----------

    def _hook_loop(self) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        self._proc_ref = HOOKPROC(self._on_hook)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc_ref,
            kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            # 装不上钩子就是热键彻底不可用，直接说清楚，别静默
            self.hook_error = ctypes.get_last_error()
            print("[热键] SetWindowsHookExW 失败, err=%d" % self.hook_error,
                  flush=True)
            return

        self.hook_installed = True
        msg = wintypes.MSG()
        # GetMessageW 返回 0 才是 WM_QUIT；-1 是错误
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _on_hook(self, nCode, wParam, lParam):
        """**必须快**。只入队，不做任何别的事（超时会被系统摘钩子）。"""
        if nCode == 0 and wParam in (WM_KEYDOWN, WM_KEYUP,
                                     WM_SYSKEYDOWN, WM_SYSKEYUP):
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if not (kb.flags & LLKHF_INJECTED):
                # 自家粘贴发的 Ctrl+V 是注入事件，上面这个过滤顺带挡掉了，
                # 不会自己触发自己
                self._queue.put((wParam in (WM_KEYUP, WM_SYSKEYUP),
                                 int(kb.vkCode), int(kb.scanCode),
                                 bool(kb.flags & LLKHF_EXTENDED)))
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # ---------- worker 线程 ----------

    def _work_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            up, vk, scan, ext = item
            self.raw_count += 1
            try:
                self.on_raw(up, vk, scan, ext)
                self._dispatch(up, vk, scan, ext)
            except Exception as exc:       # 业务回调炸了不该拖死热键
                print("[热键] 处理按键出错：%r" % (exc,), flush=True)

    def _dispatch(self, up: bool, vk: int, scan: int, ext: bool) -> None:
        # ---- Esc：录音中取消 ----
        if not up and _matches(ESC, vk, scan, ext):
            with self._lock:
                if not self._recording:
                    return
                self._recording = False
                self._cancel_timer()
            self.on_cancel()
            return

        # ---- 组合键保护（Fn 独有，必须的）----
        # Fn 现在会向 OS 发 E0 63，所以按 Fn+F1 时系统**先**看到 Fn 按下。
        # 若停够 180 ms 就会误触发录音。判据：长按计时器还挂着（说明主键
        # 才按下不到 180 ms，录音尚未开始）时，**任何别的键**按下 → 这是组合键，
        # 撤销。撤销后 _primary_held 已置 False，Fn 松开时会直接 return，不会误停。
        if not up and self._timer is not None \
                and not _matches(self.primary, vk, scan, ext):
            with self._lock:
                if self._timer is not None:
                    self._cancel_timer()
                    self._primary_held = False
            return

        # ---- 备用组合键的修饰键状态 ----
        if vk in _CTRL_VKS or vk in _ALT_VKS:
            if up:
                self._mods_down.discard(vk)
            else:
                self._mods_down.add(vk)
            # 修饰键不 return：它可能同时是主键（右 Ctrl 作主键时）

        # ---- 主键 ----
        if _matches(self.primary, vk, scan, ext):
            if not up:
                with self._lock:
                    if self._primary_held:
                        return              # 键盘自动重复，忽略
                    self._primary_held = True
                    self._cancel_timer()
                    self._timer = threading.Timer(LONG_PRESS_SECONDS,
                                                  self._fire_start)
                    self._timer.daemon = True
                    self._timer.start()
                return

            with self._lock:
                if not self._primary_held:
                    return
                self._primary_held = False
                timer_pending = self._timer is not None
                self._cancel_timer()
                was_recording = self._recording
                self._recording = False
            if timer_pending and not was_recording:
                self.on_quick_tap()         # 轻点：没到阈值
            elif was_recording:
                self.on_stop()
            return

        # ---- 备用组合键：开关式 ----
        # 对齐原版 README「Ctrl + Option + Space 开始 / 结束录音」——
        # 按一次开始，再按一次结束；松手不停。
        if vk == VK_SPACE:
            if up:
                with self._lock:
                    self._secondary_held = False
                return
            with self._lock:
                if self._secondary_held:
                    return                  # 自动重复，忽略
                if not (any(m in self._mods_down for m in _CTRL_VKS)
                        and any(m in self._mods_down for m in _ALT_VKS)):
                    return                  # 修饰键没按全
                self._secondary_held = True
                if self._recording:
                    self._recording = False
                    do_stop = True
                else:
                    do_stop = False
            if do_stop:
                self.on_stop()
            else:
                self._fire_start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire_start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
            self._timer = None
        self.on_start()


def describe_primary(spec: KeySpec) -> str:
    """给启动横幅用的一句话说明。"""
    if spec.scancode is not None:
        return "%s（扫描码 0x%02X）" % (spec.label, spec.scancode)
    return "%s（vk 0x%02X）" % (spec.label, spec.vk)
