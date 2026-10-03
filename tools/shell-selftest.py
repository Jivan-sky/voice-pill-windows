# -*- coding: utf-8 -*-
"""外壳自测：热键 + 采音 + 重采样，**不需要 ASR 引擎**。

M1 的通路是「按住 Fn → 录音 → 松手 → 出文字」。最后一步要引擎二进制，
但前面几步不需要。这个脚本把前面几步单独拎出来验，先把最容易出错的
部分（自写的 WH_KEYBOARD_LL 钩子 + WASAPI 采集 + 重采样）钉死。

用法：
    python tools/shell-selftest.py [秒数，默认 60]

验什么：
    1) Fn 按下 → 过 180 ms → 开始录音
    2) Fn 松开 → 停止录音，报「时长 / 采集字节 / WAV 大小」（三者应自洽）
    3) 轻点 Fn（<180 ms）→ 不触发
    4) Fn + F1 → **不**触发（组合键保护）
    5) 录音中按 Esc → 取消，不留 WAV
    6) Ctrl + Alt + Space → 按一次开始、再按一次结束（开关式）
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import console                     # noqa: E402

console.make_output_safe()         # 必须在任何 print 之前，否则管道下 GBK 崩

import audio as audio_mod          # noqa: E402
import config                      # noqa: E402
from hotkey import HotkeyManager, describe_primary   # noqa: E402

TARGET_RATE = 24000                 # Codex 后端要的采样率

capture = audio_mod.AudioCapture()
state = {"started": 0.0, "bytes": 0, "wav": ""}


def on_pcm(data: bytes) -> None:
    state["bytes"] += len(data)


def on_start() -> None:
    state["started"] = time.monotonic()
    state["bytes"] = 0
    state["wav"] = os.path.join(
        config.recordings_dir(), "selftest-%d.wav" % int(time.time()))
    try:
        capture.start(wav_path=state["wav"], target_rate=TARGET_RATE,
                      on_pcm=on_pcm,
                      on_error=lambda e: print("\n✖ 麦克风开流失败：%r" % (e,),
                                               flush=True))
        print("\n▶ 开始录音 …（松手结束，Esc 取消）", flush=True)
    except Exception as exc:
        print("\n✖ 麦克风打不开：%r" % (exc,), flush=True)


def on_stop() -> None:
    held = time.monotonic() - state["started"]
    capture.finish()
    size = os.path.getsize(state["wav"]) if os.path.isfile(state["wav"]) else 0
    expect = int(TARGET_RATE * held) * 2          # 16 位单声道
    print("■ 停止。按住 %.2f s" % held, flush=True)
    print("   采集 %d 字节 / 理论 %d 字节（差 %+d）"
          % (state["bytes"], expect, state["bytes"] - expect), flush=True)
    print("   WAV %d 字节（含 44 字节头）→ %s"
          % (size, state["wav"]), flush=True)
    if size <= 44:
        print("   ✖ WAV 是空的，采音链路有问题", flush=True)
    else:
        print("   ✅ 采音链路通", flush=True)


def on_cancel() -> None:
    capture.cancel()
    print("\n✖ 已取消（WAV 已删）", flush=True)


def on_quick_tap() -> None:
    print("\n· 轻点（未达 180 ms），按设计不触发", flush=True)


def _on_raw(up: bool, vk: int, scan: int, ext: bool) -> None:
    """每一次原始按键都打一行。零事件时这就是判据。"""
    if not TRACE:
        return
    mark = "[松开]" if up else "[按下]"
    print("   · %s vk=0x%02X scan=0x%02X %s"
          % (mark, vk, scan, "E0" if ext else "  "), flush=True)


TRACE = True        # --quiet 可关掉，默认开着，第一轮排查需要


def main() -> int:
    global TRACE
    duration = 60
    rest = sys.argv[1:]
    for a in rest:
        if a == "--quiet":
            TRACE = False
        elif a.isdigit():
            duration = int(a)

    settings = config.Settings.load()
    mgr = HotkeyManager(
        primary=settings.primary_spec,
        on_start=on_start, on_stop=on_stop,
        on_cancel=on_cancel, on_quick_tap=on_quick_tap,
        on_raw=_on_raw)

    print("=" * 68)
    print("外壳自测 · 主键 %s" % describe_primary(settings.primary_spec))
    print("=" * 68)
    print("请依次试：")
    print("  1) Fn 按住 2 秒再松         → 应「开始录音」然后报时长字节")
    print("  2) Fn 快速轻点一下           → 应「轻点，不触发」")
    print("  3) 按住 Fn 再按 F1           → 应**毫无反应**（组合键保护）")
    print("  4) 按 Fn 开始，录音中按 Esc  → 应「已取消」")
    print("  5) Ctrl+Alt+Space 按一次     → 开始；再按一次 → 结束")
    print("-" * 68)
    print("每按一个键都会打一行 `· [按下]/[松开] vk=.. scan=..`。")
    print("**一个 `·` 都不出 = 钩子收不到输入**，不是按键姿势问题。")
    print("=" * 68, flush=True)

    mgr.start()
    time.sleep(0.4)                     # 等钩子线程把钩子装上
    print("钩子状态：%s%s"
          % ("已装上 ✅" if mgr.hook_installed else "**没装上 ❌**",
             "" if mgr.hook_installed else "  err=%d" % mgr.hook_error),
          flush=True)

    # 心跳：每 5 秒报一次「还活着 + 已收到几条」。
    # 没有它的话，「0 条」既可能是没人按、也可能是进程早死了，分不清。
    # 有了它，输出里能看见进程确实活满了整段时间，0 条就只能是没按到。
    stop_at = time.monotonic() + duration
    nxt = time.monotonic() + 5.0
    try:
        while time.monotonic() < stop_at:
            time.sleep(min(0.2, max(0.0, stop_at - time.monotonic())))
            if time.monotonic() >= nxt:
                nxt += 5.0
                left = int(stop_at - time.monotonic())
                print("   …仍在监听，剩 %2d 秒，已收到 %d 条"
                      % (max(0, left), mgr.raw_count), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        capture.cancel()
        mgr.stop()

    print("\n" + "=" * 68)
    print("自测结束。原始按键事件共 %d 条。" % mgr.raw_count, flush=True)
    if mgr.raw_count == 0:
        print("  ❌ 一条都没收到 → 这个进程看不到交互桌面的键盘输入。")
        print("     本自测必须在你自己的终端里前台跑，挂后台会收不到。")
    print("=" * 68, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
