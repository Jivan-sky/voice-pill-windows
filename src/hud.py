# -*- coding: utf-8 -*-
"""悬浮字幕条。

对齐原版 Sources/HUD.swift 的行为，但**降级实现**：
  * 原版：NSPanel + `.screenSaver` 窗口层级 + `ignoresMouseEvents` + Liquid Glass。
  * 这里：tkinter 无边框窗口 + `-topmost` + `-alpha` 半透明 + `overrideredirect`
    （去掉标题栏、不进任务栏）。**没有真玻璃效果**——Windows 无 Liquid Glass 对应物。

原版 HUD.swift:117-147 那套「窗口层级自愈」在 Windows 上不需要：
`-topmost` 由 DWM 维护，不像 macOS 的 Space 切换会丢层级。

但原版那条 `ignoresMouseEvents` + 不抢焦点**必须补上**，而且理由比 macOS 更硬：
悬浮条一出现就把前台窗口抢走的话，粘贴前的前台校验（`paste.paste` 第 4 条）
会判定「目标窗口已切换」而放弃粘贴——文字留在剪贴板、光标处什么都没发生，
看起来就是「按了热键没反应」。见 `_harden()`。

Tk 必须在自己线程里跑，所以本模块自带线程 + 队列。所有方法线程安全。
"""
from __future__ import annotations

import ctypes
import queue
import threading
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)

BG = "#101014"
FG = "#F2F2F5"
FG_DIM = "#9A9AA5"
MAX_CHARS = 180          # 字幕过长时截断显示（原文另存，不受影响）
PAD_X, PAD_Y = 22, 14
BOTTOM_MARGIN = 96       # 距屏幕底部像素

# ---------- `_harden()` 用到的窗口样式 ----------
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000    # 出现/被点都不激活（关键的一条）
WS_EX_TOOLWINDOW = 0x00000080    # 不进 Alt+Tab
WS_EX_TRANSPARENT = 0x00000020   # 鼠标穿透：它是纯展示，不该拦点击
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL


@dataclass
class _Cmd:
    kind: str            # "show" | "hide" | "text" | "phase" | "quit"
    text: str = ""
    hint: str = ""


class HudWindow:
    """无边框半透明字幕条。show() 出现，hide() 消失。"""

    def __init__(self) -> None:
        self._q: "queue.Queue[_Cmd]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._root: Optional[tk.Tk] = None
        self._win: Optional[tk.Toplevel] = None

    # ---------- 对外（线程安全）----------

    def start(self) -> None:
        self._thread.start()

    def show(self, text: str = "", hint: str = "正在聆听…") -> None:
        """显示字幕条。hint 是下方次要行（默认沿用原版「正在聆听…」）。"""
        self._q.put(_Cmd("show", text, hint))

    def hide(self) -> None:
        self._q.put(_Cmd("hide"))

    def set_text(self, text: str) -> None:
        """更新字幕。引擎给的是整句快照，直接覆盖。"""
        self._q.put(_Cmd("text", text))

    def set_phase(self, phase: str) -> None:
        """phase: listening | transcribing（对应原版胶囊的不同视觉态）。"""
        self._q.put(_Cmd("phase", phase))

    def quit(self) -> None:
        self._q.put(_Cmd("quit"))

    # ---------- Tk 线程 ----------

    def _run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.withdraw()                      # 主窗口不要
        root.attributes("-topmost", True)

        self._win = tk.Toplevel(root)
        win = self._win
        win.overrideredirect(True)           # 无边框、不进任务栏
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        win.configure(bg=BG)
        win.withdraw()

        frame = tk.Frame(win, bg=BG, padx=PAD_X, pady=PAD_Y)
        frame.pack()

        self._label = tk.Label(
            frame, text="", bg=BG, fg=FG,
            font=("Microsoft YaHei UI", 12), justify="center",
            wraplength=560,
        )
        self._label.pack()

        self._hint = tk.Label(
            frame, text="", bg=BG, fg=FG_DIM,
            font=("Microsoft YaHei UI", 9), justify="center",
        )
        self._hint.pack(pady=(4, 0))

        root.after(60, self._pump)
        root.mainloop()

    def _pump(self) -> None:
        """主线程轮询队列，处理 UI 更新。"""
        try:
            while True:
                cmd = self._q.get_nowait()
                self._handle(cmd)
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(60, self._pump)

    def _handle(self, cmd: _Cmd) -> None:
        if self._win is None or self._root is None:
            return

        if cmd.kind == "quit":
            self._root.quit()
            return

        if cmd.kind == "hide":
            self._win.withdraw()
            return

        if cmd.kind == "show":
            self._label.config(text=self._clip(cmd.text))
            self._hint.config(text=cmd.hint or "正在聆听…")
            self._reposition()
            self._win.deiconify()
            self._win.lift()
            self._harden()
            return

        if cmd.kind == "text":
            self._label.config(text=self._clip(cmd.text))
            self._hint.config(text="正在聆听…")
            self._reposition()
            return

        if cmd.kind == "phase":
            self._hint.config(text="识别中…" if cmd.text == "transcribing"
                              else "正在聆听…")

    def _harden(self) -> None:
        """把悬浮条钉成「不激活 + 鼠标穿透」。

        每次都重设一遍，不只在建窗时设一次：Tk 的 `overrideredirect` /
        `deiconify` 会重建或调整窗口，扩展样式不是设一次就永久生效的。
        失败只当装饰没做成，绝不能影响录音主流程。
        """
        try:
            hwnd = int(self._win.winfo_id())
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                                  | WS_EX_TRANSPARENT)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:                      # noqa: BLE001
            pass

    @staticmethod
    def _clip(text: str) -> str:
        text = (text or "").strip()
        if len(text) <= MAX_CHARS:
            return text
        return "…" + text[-MAX_CHARS:]

    def _reposition(self) -> None:
        """贴屏幕底部居中。每次都重量，因为文字长度会变。"""
        win = self._win
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, sh - h - BOTTOM_MARGIN)
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))
