# -*- coding: utf-8 -*-
"""采音链路单独验：开流要多久、能不能真收到音频。

背景
----
`fn-watch.py` 实测到 `ACT stop held=0.50s bytes=0 wav=44` —— 按住了、走了长按
分支，但**一个采样字节都没落盘**。同时日志里 `ACT start` 出现在 `ACT stop`
**之后**，说明 `AudioCapture.start()` 是同步阻塞的，开设备花的时间比人按住
Fn 的时间还长。

本脚本把三件事分开量：

  1) `sd.query_devices(kind="input")` 耗时
  2) `sd.InputStream(...)` 构造 + `start()` 耗时   ← 热键"手感延迟"的真正来源
  3) 采 3 秒，报收到多少字节（24 kHz 单声道 16 位应约 144 000）

然后再验一次**常开流**方案：流一直开着，靠一个闸门决定记不记。若常开可行，
`start()` 就能做到近乎零延迟，顺带干掉 `finish()` 早于 `start()` 的竞态。

用法：
    python tools/capture-probe.py [录音秒数，默认 3]
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import numpy as np                 # noqa: E402
import sounddevice as sd           # noqa: E402

import audio as audio_mod          # noqa: E402
import config                      # noqa: E402

TARGET = 24000


def main() -> int:
    secs = 3.0
    for a in sys.argv[1:]:
        try:
            secs = float(a)
        except ValueError:
            pass

    print("=" * 68)
    print("采音链路探针")
    print("=" * 68)

    t = time.perf_counter()
    dev = sd.query_devices(kind="input")
    t_query = time.perf_counter() - t
    print("默认输入设备：%s" % dev["name"])
    print("  原生采样率 %d Hz / 最大输入声道 %d"
          % (dev["default_samplerate"], dev["max_input_channels"]))
    print("  query_devices 耗时 %.1f ms" % (t_query * 1000))

    src_rate = int(dev["default_samplerate"])
    chans = max(1, min(2, int(dev["max_input_channels"])))

    # ---- 方案 A：现开现用（当前实现）----
    print("\n[A] 现开现用（当前 audio.py 的做法）")
    got = {"n": 0}

    def cb(indata, frames, _t, status):
        got["n"] += int(frames)
        if status:
            print("   ! status=%s" % status, flush=True)

    t = time.perf_counter()
    stream = sd.InputStream(samplerate=src_rate, channels=chans, dtype="int16",
                            blocksize=1024, callback=cb)
    t_ctor = time.perf_counter() - t
    t = time.perf_counter()
    stream.start()
    t_start = time.perf_counter() - t
    print("  构造 %.1f ms + start %.1f ms = **%.1f ms** 才拿到第一帧"
          % (t_ctor * 1000, t_start * 1000, (t_ctor + t_start) * 1000))
    time.sleep(secs)
    stream.stop()
    stream.close()
    print("  %g 秒收到 %d 帧（%d Hz 原生）" % (secs, got["n"], src_rate))
    print("  → %s" % ("✅ 有音频" if got["n"] > 0 else "❌ **一帧都没有**"))

    # ---- 方案 B：常开流 + 闸门 ----
    print("\n[B] 常开流 + 闸门（建议改法）")
    gate = {"on": False, "frames": 0}
    opened = {"t": 0.0}

    def cb2(indata, frames, _t, status):
        if gate["on"]:
            gate["frames"] += int(frames)

    t = time.perf_counter()
    s2 = sd.InputStream(samplerate=src_rate, channels=chans, dtype="int16",
                        blocksize=1024, callback=cb2)
    s2.start()
    opened["t"] = time.perf_counter() - t
    print("  常开流启动耗时 %.1f ms（只在上电时付一次）" % (opened["t"] * 1000))

    time.sleep(0.3)                     # 让流跑起来
    t = time.perf_counter()
    gate["on"] = True                   # ← 这就是"按下 Fn"要做的事，纳秒级
    t_gate = time.perf_counter() - t
    time.sleep(secs)
    gate["on"] = False
    s2.stop()
    s2.close()
    print("  开闸耗时 %.6f ms（相当于 start() 的延迟）" % (t_gate * 1000))
    print("  %g 秒收到 %d 帧" % (secs, gate["frames"]))
    print("  → %s" % ("✅ 常开流可用" if gate["frames"] > 0
                      else "❌ 常开流拿不到数据"))

    # ---- 完整走一遍 AudioCapture，验重采样 + WAV ----
    print("\n[C] 走真实 AudioCapture（重采样 + WAV 落盘）")
    cap = audio_mod.AudioCapture()
    nbytes = {"n": 0}
    wav = os.path.join(config.recordings_dir(), "capture-probe.wav")
    t = time.perf_counter()
    try:
        cap.start(wav_path=wav, target_rate=TARGET,
                  on_pcm=lambda b: nbytes.__setitem__("n", nbytes["n"] + len(b)))
        t_open = time.perf_counter() - t
        print("  cap.start() 阻塞了 %.1f ms" % (t_open * 1000))
        time.sleep(secs)
    except Exception as exc:
        print("  ❌ start 失败：%r" % (exc,))
        return 1
    finally:
        cap.finish()
    size = os.path.getsize(wav) if os.path.isfile(wav) else -1
    print("  收到 %d 字节 PCM（%d Hz 单声道 16 位，%g 秒应约 %d）"
          % (nbytes["n"], TARGET, secs, int(TARGET * secs * 2)))
    print("  WAV %d 字节（头 44 字节），路径 %s" % (size, wav))
    print("  → %s" % ("✅ 采音全链路通" if nbytes["n"] > 0
                      else "❌ 一个字节都没收到"))

    # ---- 竞态回归：start() 后立刻 finish() ----
    # 这就是 fn-watch 实测到 bytes=0 时的形态。旧实现里 finish() 那一刻
    # _stream 还是 None，于是什么都没停，WAV 关成 44 字节，紧接着开流线程
    # 把流真的拉起来 → **finish() 之后数据还在源源不断地进来**。
    # 所以判据不是"WAV 多大"，而是"finish() 之后计数还涨不涨"。
    print("\n[D] 竞态回归：start() 之后**立刻** finish()")
    cap2 = audio_mod.AudioCapture()
    n2 = {"n": 0}
    err2 = []
    w2 = os.path.join(config.recordings_dir(), "race-probe.wav")
    cap2.start(wav_path=w2, target_rate=TARGET,
               on_pcm=lambda b: n2.__setitem__("n", n2["n"] + len(b)),
               on_error=err2.append)
    cap2.finish()                       # 不等开流，故意抢在前面
    after_finish = n2["n"]
    time.sleep(1.0)                     # 泄漏的话，这 1 秒里计数会一直涨
    grew = n2["n"] - after_finish
    size2 = os.path.getsize(w2) if os.path.isfile(w2) else -1
    print("  finish() 那一刻 %d 字节；随后 1 秒又涨了 %d 字节" % (after_finish, grew))
    print("  WAV %d 字节；开流错误 %s" % (size2, err2 or "无"))
    print("  流是否残留：%s" % ("是 ❌" if cap2._stream is not None else "否 ✅"))
    # 这条是 main.on_stop 那个体检闸门的输入：0 秒 = 麦克风一帧没出
    print("  seconds_captured=%.2f 秒 → 闸门判定：%s"
          % (cap2.seconds_captured,
             "跳过后端 ✅" if cap2.seconds_captured < 0.10 else "送后端"))
    print("  → %s" % ("✅ 竞态已闭合（收尾后不再有数据）" if grew == 0
                      and cap2._stream is None
                      else "❌ 仍有泄漏：收尾后数据还在进"))

    # ---- 复用同一个对象再录一次，确认没被上一次搞坏 ----
    print("\n[E] 复用同一个 AudioCapture 对象再录 1.5 秒")
    n3 = {"n": 0}
    w3 = os.path.join(config.recordings_dir(), "reuse-probe.wav")
    cap2.start(wav_path=w3, target_rate=TARGET,
               on_pcm=lambda b: n3.__setitem__("n", n3["n"] + len(b)),
               on_error=err2.append)
    time.sleep(1.5)
    cap2.finish()
    size3 = os.path.getsize(w3) if os.path.isfile(w3) else -1
    print("  1.5 秒收到 %d 字节（应约 %d），WAV %d 字节"
          % (n3["n"], int(TARGET * 1.5 * 2), size3))
    # 自洽校验：计数器必须与 on_pcm 收到的字节数**完全相等**（两者同源，
    # 差一个字节就说明计数写漏了分支）；换算出秒数供闸门比对
    print("  内部计数 %d 字节 / %.2f 秒（%s）"
          % (cap2.bytes_captured, cap2.seconds_captured,
             "✅ 与 on_pcm 一致" if cap2.bytes_captured == n3["n"]
             else "❌ 与 on_pcm 差 %d 字节" % (cap2.bytes_captured - n3["n"])))
    print("  → %s" % ("✅ 对象可复用" if n3["n"] > 0 else "❌ 第二次录不到音频"))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
