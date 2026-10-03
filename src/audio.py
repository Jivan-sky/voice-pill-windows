# -*- coding: utf-8 -*-
"""麦克风采集 + 重采样 + WAV 落盘。

对齐原版 Sources/LiveTranscriber.swift：
  * 引擎要的是**裸 PCM**：小端 16 位有符号、单声道、指定采样率。写入管道时**不带 WAV 头**。
  * 同时本地存一份 WAV，供「断线后重试」用。原版注释：「Save every frame even after the
    stream fails, for full-file retry.」
  * 原版采样率随后端变（Codex 24 kHz / 豆包 16 kHz），转换在采集回调里做。

Windows 侧差异：原版用 AVAudioEngine + AVAudioConverter。这里用 sounddevice（PortAudio/WASAPI）
拿设备原生格式，再自己降混 + 重采样到目标采样率。
"""
from __future__ import annotations

import os
import threading
import wave
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

try:
    from scipy.signal import resample_poly
    _HAVE_SCIPY = True
except ImportError:      # 没装 scipy 时退回线性插值（质量略差但能用）
    _HAVE_SCIPY = False


def _resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """int16 mono → int16 mono。"""
    if src_rate == dst_rate or pcm.size == 0:
        return pcm
    if _HAVE_SCIPY:
        from math import gcd
        g = gcd(src_rate, dst_rate)
        up, down = dst_rate // g, src_rate // g
        out = resample_poly(pcm.astype(np.float32), up, down)
    else:
        n_out = int(round(pcm.size * dst_rate / src_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.int16)
        x_old = np.linspace(0.0, 1.0, pcm.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
        out = np.interp(x_new, x_old, pcm.astype(np.float32))
    return np.clip(out, -32768, 32767).astype(np.int16)


def _close_quietly(stream) -> None:
    """关流，吞掉一切异常。

    收尾路径上不该因为"关一个已经坏掉的流"再炸一次——录音数据已经拿到了，
    关流失败只值得忽略。
    """
    try:
        stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


class AudioCapture:
    """一次录音。start() 起（**异步开流，立即返回**），finish() 收尾，cancel() 丢弃。"""

    def __init__(self) -> None:
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._wav: Optional[wave.Wave_write] = None
        self._wav_path: Optional[str] = None
        self._target_rate = 16000
        self._src_rate = 48000
        self._channels = 1
        self._on_pcm: Callable[[bytes], None] = lambda _b: None
        self._on_error: Callable[[Exception], None] = lambda _e: None

        # 开流是异步的（见 start 的注释）。这两个量负责让 finish()/cancel()
        # 能等到开流线程收尾，避免"收尾跑在开流前面"。
        self._opened = threading.Event()
        self._closing = False

        # 已采到的字节数。为什么单独计数而不看 WAV 大小：
        # 采到 0 字节这件事**必须能被调用方提前发现**。实测过一次——
        # 上一轮的僵尸进程占着麦克风，新流能开起来但一帧都收不到，于是
        # 空音频被灌进引擎，引擎如实回"收到 0 字节"，用户看到的是引擎的
        # 回显，根本判断不出是麦克风没出声。见 main.on_stop 的用法。
        self._pcm_bytes = 0

    # ---------- 查询 ----------

    @staticmethod
    def default_input_rate() -> int:
        try:
            dev = sd.query_devices(kind="input")
            return int(dev["default_samplerate"])
        except Exception:
            return 48000

    @property
    def bytes_captured(self) -> int:
        """本次录音已采到的裸 PCM 字节数（目标采样率、单声道、16 位）。"""
        with self._lock:
            return self._pcm_bytes

    @property
    def seconds_captured(self) -> float:
        """已采秒数。0 表示麦克风一帧都没出。"""
        with self._lock:
            return self._pcm_bytes / 2.0 / max(1, self._target_rate)

    @staticmethod
    def list_input_devices() -> list[tuple[int, str]]:
        out = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                out.append((idx, dev["name"]))
        return out

    # ---------- 生命周期 ----------

    def start(self, wav_path: str, target_rate: int,
              on_pcm: Callable[[bytes], None],
              on_error: Optional[Callable[[Exception], None]] = None) -> None:
        """开始采集。**立即返回**，开设备在后台线程里做。

        on_pcm 收到的是**目标采样率下的裸 PCM 字节**，直接灌引擎。

        为什么异步
        ----------
        `sd.InputStream(...)` 的构造是同步阻塞的：实测冷启动 161 ms、
        `cap.start()` 整体 122 ms。而调用方是「按住 Fn 180 ms 后才起录」的
        热键线程，人一松手就 `finish()`。同步开流会被收尾抢在前面，实测后果是
        **收尾什么都没停掉（那一刻 `_stream` 还是 None）、WAV 已关成 44 字节，
        随后开流线程把流真的拉起来，留下一个永远在采、但没人在写的泄漏流**。
        日志里表现为 `ACT stop ... bytes=0 wav=44` 之后才出现 `ACT start`。

        所以：开流丢线程，`finish()`/`cancel()` 先置 `_closing` 再等
        `_opened`。开流线程发现 `_closing` 已置就把刚建好的流就地关掉，
        绝不交给 `_stream`——竞态的窗口由此闭合。
        """
        if self._stream is not None:
            raise RuntimeError("上一次录音还没收尾，先 finish()/cancel()")

        self._target_rate = target_rate
        self._on_pcm = on_pcm
        self._on_error = on_error or (lambda _e: None)
        self._wav_path = wav_path
        os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)

        dev_info = sd.query_devices(kind="input")     # 实测 0.1 ms，可以同步做
        self._src_rate = int(dev_info["default_samplerate"])
        # 设备原生声道数（多数 48k 立体声）；单声道设备则为 1
        self._channels = max(1, min(2, int(dev_info.get("max_input_channels", 1))))

        with self._lock:
            self._closing = False
            self._opened.clear()
            self._pcm_bytes = 0
            self._wav = wave.open(wav_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)                 # 16 bit
            self._wav.setframerate(target_rate)

        threading.Thread(target=self._open_worker, daemon=True).start()

    def _open_worker(self) -> None:
        """后台开流。**唯一**写 self._stream 的地方（除 _stop_stream 清空）。"""
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=self._src_rate,
                channels=self._channels,
                dtype="int16",
                blocksize=1024,
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            with self._lock:
                self._closing = True
            if stream is not None:
                _close_quietly(stream)
            self._opened.set()            # 必须先放行，再回调，免得回调里再等
            self._on_error(exc)
            return

        with self._lock:
            if self._closing:
                # 收尾先到了：这个流是没人要的，就地关掉，别挂到 self._stream
                _close_quietly(stream)
            else:
                self._stream = stream
        self._opened.set()

    def _callback(self, indata, _frames, _time, status) -> None:
        # status 里可能有 overflow 等提示；不阻断采集，静默继续（录音优先）
        pcm = np.asarray(indata)
        if pcm.ndim > 1 and pcm.shape[1] > 1:
            # 降混：取各声道均值
            pcm = pcm.mean(axis=1)
        else:
            pcm = pcm.reshape(-1)

        mono = _resample(pcm.astype(np.int16), self._src_rate, self._target_rate)
        if mono.size == 0:
            return

        with self._lock:
            # 先落盘再送管道：原版语义是「即使流失败也保存每一帧」
            if self._wav is not None:
                try:
                    self._wav.writeframes(mono.tobytes())
                except (OSError, ValueError):
                    pass
            self._pcm_bytes += mono.size * 2
        self._on_pcm(mono.tobytes())

    def finish(self) -> Optional[str]:
        """停止采集并收尾 WAV。返回 WAV 路径（供重试），失败返回 None。"""
        self._settle()
        with self._lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except (OSError, ValueError):
                    pass
                self._wav = None
        return self._wav_path

    def cancel(self) -> None:
        """丢弃：停流、关文件，不留 WAV。"""
        self._settle()
        with self._lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except (OSError, ValueError):
                    pass
                self._wav = None
        if self._wav_path and os.path.isfile(self._wav_path):
            try:
                os.remove(self._wav_path)
            except OSError:
                pass

    def _settle(self) -> None:
        """收尾前的统一动作：拦下还没建完的流，再停掉已建好的。

        先置 `_closing` 再等 `_opened`，顺序不能反——反了就是原来那个竞态。
        等不到（5 秒）说明开流线程卡死，此时也必须往下走：宁可丢一段录音，
        不能让热键线程永久挂住。
        """
        with self._lock:
            self._closing = True
        self._opened.wait(5.0)
        self._stop_stream()

    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            _close_quietly(stream)
