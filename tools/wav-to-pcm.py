# -*- coding: utf-8 -*-
"""WAV → 裸 PCM16LE 单声道，供 `codex-asr stream` / `freeasr live` 直接吃。

引擎要的是**裸 PCM**，不带 WAV 头、不带容器（这是进程契约的一部分）。
本脚本把任意 WAV 降成契约要求的格式。

用法：
    python tools/wav-to-pcm.py <输入.wav> <输出.pcm> [采样率，默认 24000]
"""
import sys
import wave

import numpy as np

src = sys.argv[1]
dst = sys.argv[2]
rate = int(sys.argv[3]) if len(sys.argv) > 3 else 24000

with wave.open(src, "rb") as w:
    ch, width, sr, n = (w.getnchannels(), w.getsampwidth(),
                        w.getframerate(), w.getnframes())
    raw = w.readframes(n)

print("输入：%d 通道 / %d 位 / %d Hz / %d 帧 / %.2f 秒"
      % (ch, width * 8, sr, n, n / float(sr)))

if width != 2:
    raise SystemExit("只支持 16 位 WAV，实际 %d 位" % (width * 8))

a = np.frombuffer(raw, dtype="<i2")
if ch > 1:                                  # 多声道 → 取平均降成单声道
    a = a.reshape(-1, ch).mean(axis=1)
a = a.astype(np.float64)

if sr != rate:
    # 与 src/audio.py 同样的做法：多相滤波重采样
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(sr, rate)
    a = resample_poly(a, rate // g, sr // g)
    print("重采样：%d Hz → %d Hz" % (sr, rate))

pcm = np.clip(np.round(a), -32768, 32767).astype("<i2").tobytes()
with open(dst, "wb") as f:
    f.write(pcm)

print("输出：%s  %d 字节  %.2f 秒 @ %d Hz 单声道"
      % (dst, len(pcm), len(pcm) / 2.0 / rate, rate))
