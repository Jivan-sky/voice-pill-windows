# -*- coding: utf-8 -*-
"""会话里到底有没有物理输入？——不用任何人按键也能判定。

问题
----
`shell-selftest.py` 跑满 60 秒、心跳证明进程活着、钩子 `hook_installed=True`，
可原始按键事件 **0 条**。两种解释：

  A) 那 60 秒里真没人按键；
  B) 物理输入根本没投递到这个会话/进程。

之前分不开，因为"有没有人按键"这个变量掌握在人手里。

办法
----
`GetLastInputInfo` 返回**整个会话**最后一次输入事件的时间戳（键鼠都算）。
它不依赖钩子、不依赖谁被按了什么键，只要有人在动鼠标就会跳。于是：

  · 时间戳在跳 → 会话里有物理输入在流动
        → 此时若钩子仍是 0 条，就是 B：**钩子收不到物理输入**，
          而这不是"没人按"能解释的，必须换启动方式（见结论）。
  · 时间戳不动 → 那段时间整个会话都没人碰键鼠
        → 0 条不能作数，得让人在窗口期内按几下重测。

判定表（和钩子计数一起看）：

  LastInputInfo 动 | 钩子计数 | 结论
  -----------------|---------|----------------------------------------
  动               | > 0     | ✅ 全通
  动               | = 0     | ❌ 钩子被隔离，进程看不到物理输入
  不动             | = 0     | ⚠️ 这段时间没人碰键鼠，本轮无效，重测
  不动             | > 0     | 理论上不该出现（钩子收到了却没记进 session）

用法：
    python tools/input-liveness.py [秒数，默认 60]

跑起来后**随便动几下鼠标**即可，不必按键。鼠标一动，本脚本就能给出结论。
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

import config                      # noqa: E402
from hotkey import HotkeyManager   # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def last_input_tick() -> int:
    """会话最后一次输入事件的 tick（毫秒）。取不到返回 -1。"""
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO), dwTime=0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return -1
    return int(info.dwTime)


def main() -> int:
    duration = 60
    for a in sys.argv[1:]:
        if a.isdigit():
            duration = int(a)

    settings = config.Settings.load()

    events = []
    mgr = HotkeyManager(
        primary=settings.primary_spec,
        on_raw=lambda up, vk, scan, ext: events.append((up, vk, scan, ext)))
    mgr.start()
    time.sleep(0.4)

    print("=" * 68)
    print("输入存活性判定 · 跑 %d 秒。**动几下鼠标**就行，不用按键。" % duration)
    print("=" * 68)
    print("钩子状态：%s" % ("已装上 ✅" if mgr.hook_installed
                            else "**没装上 ❌ err=%d**" % mgr.hook_error),
          flush=True)
    print("-" * 68, flush=True)

    t0 = time.monotonic()
    stop_at = t0 + duration
    prev = last_input_tick()
    # 换算：GetLastInputInfo 的 dwTime 是 GetTickCount 体系（系统开机毫秒）
    idle_at_start = max(0, int(kernel32.GetTickCount()) - prev) if prev >= 0 else -1
    print("起始：距上次输入 %s ms" % (idle_at_start if idle_at_start >= 0 else "?"),
          flush=True)
    moved = 0
    try:
        while time.monotonic() < stop_at:
            time.sleep(0.25)
            cur = last_input_tick()
            if cur != prev and cur >= 0:
                moved += 1
                idle = max(0, int(kernel32.GetTickCount()) - cur)
                print("   ⚡ 会话有新输入（第 %d 次），距现在 %d ms；"
                      "钩子已收到 %d 条" % (moved, idle, mgr.raw_count), flush=True)
                prev = cur
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop()

    print("-" * 68)
    print("会话输入事件：%d 次      钩子原始按键：%d 条" % (moved, mgr.raw_count))
    if moved > 0 and mgr.raw_count > 0:
        print("→ ✅ 全通。钩子能看到物理输入。")
    elif moved > 0 and mgr.raw_count == 0:
        print("→ ❌ **会话有输入流动，钩子却一条没收到** → 这个进程的钩子")
        print("     收不到物理输入（注入事件能收到，见 desktop-probe.py）。")
        print("     不是「没人按键」的问题。")
    else:
        print("→ ⚠️ 这段时间整个会话都没人碰键鼠，本轮**无效**。")
        print("     请动一下鼠标再跑一次。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
