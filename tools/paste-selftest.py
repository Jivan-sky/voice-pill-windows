# -*- coding: utf-8 -*-
"""粘贴自测：自己开一个文本框当靶子，不碰用户正在用的窗口。

为什么要有靶子
--------------
真跑粘贴会往**当前前台窗口**注入 Ctrl+V。在自动化环境里那意味着把测试文字
塞进用户当时正开着的编辑器/输入框——污染人家的内容，不能这么干。

所以这里自己建一个 tkinter 文本框，把它弄成前台窗口，再调 `paste.paste()`
真正走一遍：剪贴板写入 → 等修饰键松开 → 前台窗口比对 → SendInput Ctrl+V
→ 延迟还原剪贴板。末尾直接读文本框内容，看文字到底进没进去。

怎么把窗口弄成前台
------------------
`SetForegroundWindow` 对非前台进程有限制，`focus_force()` 经常静默失败。
可靠办法是**自己给自己点一下**：SendInput 鼠标移到窗口中心点一下，本进程就
成了前台进程，`SetForegroundWindow` 随之生效。注入本身在 desktop-probe.py
里已经验过是通的。

用法：
    python tools/paste-selftest.py
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import tkinter as tk               # noqa: E402

import paste as paste_mod          # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetForegroundWindow.restype = ctypes.c_void_p

SENTINEL = "剪贴板哨兵-ORIGINAL-42"
TEXT = "Voice Pill 粘贴自测：中文 ABC 123 ✓"


def _click_at(x: int, y: int) -> None:
    """在 (x, y) 注入一次左键单击。用 paste.INPUT / MOUSEINPUT，不另建结构体。"""
    MOVE, DOWN, UP = 0x0001, 0x0002, 0x0004
    evs = []
    for flags in (MOVE, DOWN, UP):
        ev = paste_mod.INPUT()
        ev.type = 0                       # INPUT_MOUSE
        ev.u.mi = paste_mod.MOUSEINPUT(dx=x, dy=y, mouseData=0, dwFlags=flags,
                                       time=0, dwExtraInfo=None)
        evs.append(ev)
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    arr = (paste_mod.INPUT * len(evs))(*evs)
    n = user32.SendInput(len(evs), arr, ctypes.sizeof(paste_mod.INPUT))
    print("   注入鼠标事件 %d/%d" % (n, len(evs)))


def main() -> int:
    print("=" * 68)
    print("粘贴自测")
    print("=" * 68)

    # ---- 1. 剪贴板读写往返 ----
    print("\n[1] 剪贴板读写往返（含中文与符号）")
    original = paste_mod.get_clipboard_text()
    print("   原剪贴板：%r" % (original,))
    if not paste_mod.set_clipboard_text(SENTINEL):
        print("   ❌ 写剪贴板失败")
        return 1
    back = paste_mod.get_clipboard_text()
    ok = back == SENTINEL
    print("   写→读：%r  %s" % (back, "✅" if ok else "❌ 不一致"))
    if not ok:
        return 1
    seq0 = paste_mod.clipboard_sequence()
    print("   剪贴板序号：%d" % seq0)

    # ---- 2. 修饰键轮询 ----
    print("\n[2] 等修饰键松开")
    t = time.monotonic()
    released = paste_mod.wait_for_modifier_release()
    print("   %s（耗时 %.0f ms）"
          % ("✅ 无修饰键按住" if released else "⚠️ 超时仍有修饰键按住",
             (time.monotonic() - t) * 1000))
    if not released:
        print("   → 后面注入的 Ctrl+V 会被目标程序当成 Ctrl+X+V 之类，"
              "先松开所有修饰键再跑")

    # ---- 3. 建靶子窗口 ----
    print("\n[3] 建靶子窗口并弄成前台")
    root = tk.Tk()
    root.title("Voice Pill 粘贴靶子")
    root.geometry("560x220+220+180")
    text = tk.Text(root, font=("Microsoft YaHei", 12))
    text.pack(fill="both", expand=True)
    root.update()
    time.sleep(0.3)
    root.update()

    hwnd = int(root.wm_frame(), 16)       # 顶层框架窗口句柄
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.update()

    fg = int(user32.GetForegroundWindow() or 0)
    if fg != hwnd:
        # focus_force 常被系统拒绝，自己点自己一下
        x = root.winfo_rootx() + root.winfo_width() // 2
        y = root.winfo_rooty() + root.winfo_height() // 2
        print("   focus_force 没拿到前台，改为自注入单击 (x=%d, y=%d)" % (x, y))
        for _ in range(3):
            _click_at(x, y)
            time.sleep(0.25)
            root.update()
            fg = int(user32.GetForegroundWindow() or 0)
            if fg == hwnd:
                break
    print("   靶子 hwnd=0x%X  当前前台=0x%X  %s"
          % (hwnd, fg, "✅ 已在前台" if fg == hwnd else "❌ 仍不是前台"))
    if fg != hwnd:
        print("   → 前台抢不到，粘贴里的窗口比对必然失败。本机环境下此项需人工验。")
        root.destroy()
        paste_mod.set_clipboard_text(original or "")
        return 1

    text.focus_set()
    root.update()

    # ---- 4. 真正走一遍 paste.paste() ----
    print("\n[4] 调 paste.paste()（剪贴板 → 比对前台 → Ctrl+V → 延迟还原）")
    t = time.monotonic()
    try:
        paste_mod.paste(TEXT, hwnd)
    except paste_mod.PasteError as exc:
        print("   ❌ PasteError: %s" % exc)
        root.destroy()
        return 1
    print("   paste() 返回，耗时 %.0f ms" % ((time.monotonic() - t) * 1000))

    # 文本框内容由 Tk 自己处理 <<Paste>> 后才出现，等一会儿并持续 pump 事件
    landed = ""
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        root.update()
        landed = text.get("1.0", "end").strip()
        if landed:
            break
        time.sleep(0.05)
    print("   文本框内容：%r" % landed)
    print("   → %s" % ("✅ Ctrl+V 真的落到了目标窗口" if landed == TEXT
                      else "❌ 文字没进去"))

    # ---- 5. 剪贴板有没有还原 ----
    print("\n[5] 延迟还原剪贴板")
    time.sleep(paste_mod.PASTE_COMPLETION + 0.6)
    now = paste_mod.get_clipboard_text()
    print("   现在剪贴板：%r（期望 %r）" % (now, SENTINEL))
    print("   → %s" % ("✅ 已还原" if now == SENTINEL else "❌ 没还原"))

    root.destroy()
    # 收尾：把用户原来的剪贴板还回去
    if original is not None:
        paste_mod.set_clipboard_text(original)
    print("\n   剪贴板已恢复成运行前的值。")
    print("=" * 68)
    return 0 if landed == TEXT else 1


if __name__ == "__main__":
    sys.exit(main())
