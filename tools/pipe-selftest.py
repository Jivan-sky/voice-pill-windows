# -*- coding: utf-8 -*-
"""管道自测：拿一个 WAV 直接喂引擎，把「采音」和「热键」摘出去。

为什么需要它
------------
全链路自测（`main.py --once`）出问题时分不清是哪一段坏的：麦克风？热键？
管道？解码？引擎？每验一次都要人按一次 Fn、说一句话，成本高、变量多。

本脚本把 WAV 文件当成"刚录好的音"，走**和 main.py 完全相同的**那几行：
    config.engine_path → LiveTranscriptionSession.start
    → append(裸 PCM) → finish() → on_complete(文本, 错误)
于是管道、NDJSON 解码、背压、超时、退出码判定全都被覆盖，而**不需要麦克风、
不需要热键、不需要人**。剩下没覆盖的只有采音和粘贴两段。

用法：
    python tools/pipe-selftest.py <wav 路径> [backend]
    python tools/pipe-selftest.py            # 不传则用 recordings/ 里最新的 wav

backend 默认取 settings.json 里的；也可以显式给 codex / doubao。
引擎路径仍受 `VOICEPILL_ENGINE` 影响 —— 自测假引擎：
    VOICEPILL_ENGINE=tools/mock-asr.py python tools/pipe-selftest.py
"""
from __future__ import annotations

import glob
import os
import sys
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import numpy as np                 # noqa: E402
import audio as audio_mod          # noqa: E402
import config                      # noqa: E402
import providers                   # noqa: E402
from live_session import LiveTranscriptionSession   # noqa: E402

CHUNK_MS = 100                     # 按 100 ms 一块灌，模拟实时节奏


def newest_wav() -> str:
    files = glob.glob(os.path.join(config.recordings_dir(), "*.wav"))
    if not files:
        return ""
    return max(files, key=os.path.getmtime)


def read_pcm(path: str, target_rate: int) -> bytes:
    """WAV → 目标采样率的裸 PCM16LE 单声道（复用 audio._resample，和实采同一条路径）。"""
    with wave.open(path, "rb") as wf:
        src_rate = wf.getframerate()
        chans = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise SystemExit("只支持 16 位 WAV，这个文件是 %d 位" % (width * 8))
    pcm = np.frombuffer(raw, dtype=np.int16)
    if chans > 1:
        pcm = pcm.reshape(-1, chans).mean(axis=1).astype(np.int16)
    return audio_mod._resample(pcm, src_rate, target_rate).tobytes()


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    wav = argv[0] if argv else newest_wav()
    backend = argv[1] if len(argv) > 1 else config.Settings.load().provider

    if not wav or not os.path.isfile(wav):
        print("找不到 WAV。给个路径，或先录一段到 %s" % config.recordings_dir())
        return 1

    provider = providers.get(backend)
    engine = config.engine_path(provider.binary)
    print("=" * 68)
    print("管道自测")
    print("=" * 68)
    print("  音频    ：%s（%d 字节）" % (wav, os.path.getsize(wav)))
    print("  后端    ：%s" % provider.label)
    print("  引擎    ：%s%s" % (engine, "  ← VOICEPILL_ENGINE 顶替"
                                if os.environ.get("VOICEPILL_ENGINE") else ""))
    if not os.path.isfile(engine) and not os.environ.get("VOICEPILL_ENGINE"):
        print("  ❌ 引擎不存在")
        return 1

    pcm = read_pcm(wav, provider.sample_rate)
    print("  裸 PCM  ：%d 字节 → %.2f 秒 @ %d Hz 单声道 16 位"
          % (len(pcm), len(pcm) / 2.0 / provider.sample_rate,
             provider.sample_rate))

    done = {}
    updates = []

    def on_update(text: str) -> None:
        updates.append(text)
        print("  · 中间结果：%s" % text[:70], flush=True)

    def on_complete(text, error) -> None:
        done["text"] = text
        done["error"] = error

    log_path = os.path.join(config.logs_dir(),
                            "pipe-selftest-%d.log" % int(time.time()))
    session = LiveTranscriptionSession()
    t0 = time.monotonic()
    session.start(binary_path=engine, provider=provider, log_path=log_path,
                  punctuation=True, on_update=on_update,
                  on_complete=on_complete)

    step = max(2, provider.sample_rate * 2 * CHUNK_MS // 1000)   # 100 ms 字节数
    for off in range(0, len(pcm), step):
        session.append(pcm[off:off + step])
        time.sleep(CHUNK_MS / 1000.0)
    session.finish()

    deadline = time.monotonic() + provider.finish_timeout + 5
    while "text" not in done and time.monotonic() < deadline:
        time.sleep(0.05)

    elapsed = time.monotonic() - t0
    print("-" * 68)
    if "text" not in done:
        print("❌ 超时：引擎没在 %.0f 秒内给出完成事件" % provider.finish_timeout)
        return 1
    if done["error"]:
        print("❌ 失败：%s" % done["error"])
        if os.path.isfile(log_path):
            with open(log_path, "rb") as fh:
                tail = fh.read().decode("utf-8", "replace").strip()
            if tail:
                print("   引擎 stderr 尾部：\n%s" % tail[-1200:])
        return 1

    print("✅ 成功，耗时 %.2f 秒" % elapsed)
    print("   完成事件文本：%s" % done["text"])
    print("   中间结果 %d 条" % len(updates))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
