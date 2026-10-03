# -*- coding: utf-8 -*-
"""本地离线引擎：sherpa-onnx + SenseVoice，实现 NDJSON 契约。

为什么有它
----------
Codex 后端要 ChatGPT 付费订阅换来的 OAuth 令牌；豆包那条非官方协议
2026-10-03 实测已确认服务端失联（见 docs/移植方案.md 6.4）。本地引擎不
依赖账号、不依赖外部服务，是这两条都走不通时的底座。

契约（与 tools/mock-asr.py、真引擎完全一致，见 src/live_session.py）
------------------------------------------------------------------
    stdin  ← 裸 PCM16LE 单声道 @ --sample-rate，一直灌到收工
    stdout → NDJSON，每行一条、UTF-8、行尾必须有反斜杠 n：
        {"type": "ready"}
        {"type": "partial", "text": "<到目前的全文快照>"}   ← 可发多次
        {"type": "final",   "text": "<最终全文>"}
        {"type": "result",  "text": "<最终全文>"}
    stderr → 人看的日志（父进程落盘，失败时读尾部）

text 是**整句快照**不是增量，父进程直接覆盖。

实时字幕怎么来的
----------------
SenseVoice 不是流式模型，但快得离谱：本机实测 RTF 0.027（8.93 秒音频
0.23 秒出结果）。所以这里用「隔一会儿把已收到的音频整段重跑一次」的笨
办法换中间结果 —— 实测截前 3 秒能出「这是一段测试语音，用来验证。」，够用。

重跑间隔自适应：按上一次解码耗时的 4 倍退避。固定间隔必然追不上 —— 60 秒
音频单次解码约 1.6 秒，1 秒的间隔会越跑越落后。超过 --max-partial-seconds
就只收音频、不再出中间结果，免得白烧 CPU。

用法（一般由 src/providers.py 的 LocalProvider 自动拼装，不用手敲）
----------------------------------------------------------------
    python tools/local-asr.py --sample-rate 16000 --model-dir <模型目录>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import numpy as np

# 短于这个秒数的音频不值得跑一次识别：出来的也是噪声
MIN_PARTIAL_SECONDS = 0.3
READ_CHUNK = 1 << 16
# 缓冲上限（秒）。到顶就不再收 —— 宁可丢超长录音的尾巴，也不让内存无界涨
MAX_BUFFER_SECONDS = 300.0


def force_utf8() -> None:
    """stdout 钉死 UTF-8。

    管道接走后 Python 会退回 locale 编码（本机 GBK），中文事件直接把父进程
    的解码器炸成 utf-8 解码错误。父进程虽然会塞 PYTHONUTF8，但引擎自己也
    得守规矩 —— 它是被用来验契约的那一端。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class AudioBuffer:
    """读线程写、识别线程读的累积缓冲。"""

    def __init__(self, rate: int) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._limit = int(rate * 2 * MAX_BUFFER_SECONDS)
        # 收工时置位，让识别循环立刻醒，不必等满一个间隔
        self.done = threading.Event()

    def reader(self) -> None:
        fd = sys.stdin.fileno()
        while True:
            try:
                chunk = os.read(fd, READ_CHUNK)      # 短读：有多少拿多少
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                room = self._limit - len(self._buf)
                if room > 0:
                    self._buf.extend(chunk[:room])
        self.done.set()

    def size(self) -> int:
        with self._lock:
            return len(self._buf)

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)


class LocalRecognizer:
    """SenseVoice 的薄封装。只在一个线程里调用 —— 免得猜 sherpa 的线程安全性。"""

    def __init__(self, model_dir: str, rate: int, language: str,
                 threads: int, itn: bool) -> None:
        import sherpa_onnx
        self.rate = rate
        if threads <= 0:
            threads = min(8, os.cpu_count() or 4)
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(model_dir, "model.int8.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            num_threads=threads,
            use_itn=itn,
            language=language,
            debug=False,
        )

    def transcribe(self, pcm: bytes):
        """裸 PCM16LE → (文本, 解码耗时秒)。"""
        usable = len(pcm) // 2 * 2               # 丢掉可能多出来的半个采样
        samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)
        samples /= 32768.0
        t0 = time.monotonic()
        stream = self._rec.create_stream()
        stream.accept_waveform(self.rate, samples)
        self._rec.decode_stream(stream)
        return stream.result.text, time.monotonic() - t0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Voice Pill 本地离线转写引擎")
    ap.add_argument("--sample-rate", type=int, default=16000)
    ap.add_argument("--model-dir", default=os.environ.get("VOICEPILL_LOCAL_MODEL", ""),
                    help="含 model.int8.onnx 与 tokens.txt 的目录")
    ap.add_argument("--language", default="zh", help="zh/en/ja/ko/yue，空串为自动")
    ap.add_argument("--threads", type=int, default=0, help="0 = 自动")
    ap.add_argument("--partial-interval", type=float, default=1.0,
                    help="中间结果的最小间隔（秒），实际会按解码耗时退避")
    ap.add_argument("--max-partial-seconds", type=float, default=120.0,
                    help="音频超过这个长度就不再出中间结果")
    ap.add_argument("--no-punctuation", action="store_true",
                    help="关掉标点与数字规整（对应 use_itn=False）")
    return ap


def main() -> int:
    force_utf8()
    args = build_parser().parse_args()

    model_dir = args.model_dir
    needed = (os.path.join(model_dir, "model.int8.onnx"),
              os.path.join(model_dir, "tokens.txt"))
    missing = [p for p in needed if not os.path.isfile(p)]
    if missing:
        for path in missing:
            print("缺文件：%s" % path, file=sys.stderr)
        print("模型目录：%s（--model-dir 或 VOICEPILL_LOCAL_MODEL）" % model_dir,
              file=sys.stderr)
        emit({"type": "error"})
        return 2

    # 先把读线程跑起来再加载模型：加载约 1.1 秒，正好和用户开口的时间重叠
    buf = AudioBuffer(args.sample_rate)
    threading.Thread(target=buf.reader, daemon=True).start()
    emit({"type": "ready"})

    try:
        rec = LocalRecognizer(model_dir, args.sample_rate, args.language,
                              args.threads, not args.no_punctuation)
    except Exception as exc:                     # 模型损坏、缺依赖等
        print("模型加载失败：%r" % (exc,), file=sys.stderr)
        emit({"type": "error"})
        return 3

    min_bytes = int(args.sample_rate * 2 * MIN_PARTIAL_SECONDS)
    max_partial_bytes = int(args.sample_rate * 2 * args.max_partial_seconds)

    last_text = ""
    delay = args.partial_interval
    while not buf.done.wait(delay):
        size = buf.size()
        if size < min_bytes or size > max_partial_bytes:
            continue
        text, cost = rec.transcribe(buf.snapshot())
        delay = max(args.partial_interval, cost * 4.0)   # 退避，别越跑越落后
        if text and text != last_text:
            last_text = text
            emit({"type": "partial", "text": text})

    text, cost = rec.transcribe(buf.snapshot())
    seconds = buf.size() / 2.0 / args.sample_rate
    if text:
        print("收工：音频 %.2f 秒，收尾解码 %.2f 秒" % (seconds, cost),
              file=sys.stderr)
    else:
        print("收工：音频 %.2f 秒，没识别出文字" % seconds, file=sys.stderr)

    # 两个后端认的完成事件不同：Codex 认 result，豆包/本地认 final。
    # 都发，谁挂上来都能收尾。
    emit({"type": "final", "text": text})
    emit({"type": "result", "text": text})
    return 0


if __name__ == "__main__":
    sys.exit(main())
