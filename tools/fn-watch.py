# -*- coding: utf-8 -*-
"""盯着 Fn，只报要紧的几行。给人留足时间，不用掐着秒表按。

为什么另起一个脚本
------------------
`shell-selftest.py` 把**所有**按键都打出来。实测跑 60 秒会刷 134 行——因为
被测的人往往正在往 Claude Code 输入框里打字，字母键淹没了真正要看的 Fn。
而且它要人在固定的 60 秒窗口里按，错过就白跑。

本脚本反过来：
  * 只打三类行：`READY`、**命中主键**、动作（开始/停止/取消/轻点）。
  * 跑得久（默认 900 秒），随时按都算数。

用法：
    python tools/fn-watch.py [秒数，默认 900]

输出行（供上层做事件流用，格式是契约，别改）：
    READY primary=Fn(scan=0x63) hook=ok
    FN down  vk=0xFF scan=0x63 ext=1
    FN up    vk=0xFF scan=0x63 ext=1
    ACT start | ACT stop held=2.13s bytes=102336 wav=... | ACT cancel | ACT tap
    HB count=12 fn=4
    DONE total=40 fn=4
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import audio as audio_mod          # noqa: E402
import config                      # noqa: E402
from hotkey import HotkeyManager, describe_primary   # noqa: E402

settings = config.Settings.load()
capture = audio_mod.AudioCapture()
st = {"t0": 0.0, "bytes": 0, "wav": "", "fn": 0, "total": 0}


def say(line: str) -> None:
    print(line, flush=True)


def on_raw(up: bool, vk: int, scan: int, ext: bool) -> None:
    st["total"] += 1
    # 只放行主键（Fn）。别的键一律吞掉，免得被打字刷屏。
    spec = settings.primary_spec
    hit = (scan == spec.scancode and bool(ext) == spec.extended
           if spec.scancode is not None else vk == spec.vk)
    if not hit:
        return
    st["fn"] += 1
    if up:
        # 打印**实测**按住时长。没有它就没法区分两种"轻点"：
        #   人真的点了一下  vs  Fn 的抬起本来就提前到（固件行为）。
        # 前者 held 会到 0.5 s 以上却仍走 tap 分支 → 那是代码 bug。
        held = time.monotonic() - st["t_down"] if st["t_down"] else -1.0
        say("FN up    vk=0x%02X scan=0x%02X ext=%d held=%.3fs"
            % (vk, scan, int(ext), held))
        st["t_down"] = 0.0
    else:
        st["t_down"] = time.monotonic()
        say("FN down  vk=0x%02X scan=0x%02X ext=%d" % (vk, scan, int(ext)))


def on_pcm(data: bytes) -> None:
    st["bytes"] += len(data)


def on_start() -> None:
    st["t0"] = time.monotonic()
    st["bytes"] = 0
    st["wav"] = os.path.join(config.recordings_dir(),
                             "fnwatch-%d.wav" % int(time.time()))
    try:
        # start() 立即返回，开设备在后台。开流失败走 on_error。
        capture.start(wav_path=st["wav"], target_rate=24000, on_pcm=on_pcm,
                      on_error=lambda e: say("ACT mic-FAILED %r" % (e,)))
        say("ACT start")
    except Exception as exc:
        say("ACT start-FAILED %r" % (exc,))


def on_stop() -> None:
    held = time.monotonic() - st["t0"]
    capture.finish()
    size = os.path.getsize(st["wav"]) if os.path.isfile(st["wav"]) else 0
    say("ACT stop held=%.2fs bytes=%d wav=%d"
        % (held, st["bytes"], size))


def on_cancel() -> None:
    capture.cancel()
    say("ACT cancel")


def on_quick_tap() -> None:
    say("ACT tap")


def main() -> int:
    duration = 900
    for a in sys.argv[1:]:
        if a.isdigit():
            duration = int(a)

    mgr = HotkeyManager(primary=settings.primary_spec, on_start=on_start,
                        on_stop=on_stop, on_cancel=on_cancel,
                        on_quick_tap=on_quick_tap, on_raw=on_raw)
    mgr.start()
    time.sleep(0.4)

    say("READY primary=%s hook=%s"
        % (describe_primary(settings.primary_spec),
           "ok" if mgr.hook_installed else "FAILED err=%d" % mgr.hook_error))

    stop_at = time.monotonic() + duration
    nxt = time.monotonic() + 180.0        # 心跳 3 分钟一次，够证明还活着
    try:
        while time.monotonic() < stop_at:
            time.sleep(0.2)
            if time.monotonic() >= nxt:
                nxt += 180.0
                say("HB count=%d fn=%d" % (st["total"], st["fn"]))
    except KeyboardInterrupt:
        pass
    finally:
        capture.cancel()
        mgr.stop()

    say("DONE total=%d fn=%d" % (st["total"], st["fn"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
