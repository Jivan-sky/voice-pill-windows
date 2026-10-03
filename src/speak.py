# -*- coding: utf-8 -*-
"""离线 TTS：让 Codex 的话在这台机器上**出声**。

它解决什么
----------
在此之前全链路是**单向**的：你说 → 出字。Codex 的回复只有屏幕上那一份，
人得盯着看。这个模块补上反方向——助手说完，念给你听。

定位
----
* **完全本机**：模型在 D 盘（和 ASR 模型并排，见 `config.tts_model_dir()`），
  合成走 sherpa-onnx 的 `OfflineTts`（Kokoro 多语言 v1.1，中英混读），
  播放走 WASAPI。不联网、不需要账号——和 ASR 后端的定案一致。定这条是有
  代价的（云 TTS 更省事、音色更多），但"运行时不依赖网络与凭据"这条线
  一旦破，整个工具就会在断网/换机时变成半成品。
* **不是另起一个服务**：它是常驻实例的一个能力，从命名管道的 `speak` 命令
  进来，和 `start`/`stop`/`take` 同一层。所以单实例、看门狗、日志、护栏
  这些外壳全都白拿。

三个实测依据（数字见 `docs/移植方案.md` 第 16 节）
------------------------------------------------
1. **懒加载**：模型 325 MB、加载 1.9 秒。常驻进程不该为一个可能用不上的能力
   开机就多背这些内存，所以第一次真要说话时才加载。
2. **线程数 8**：实测 RTF —— 2 线程 0.63、8 线程 **0.43**、18 线程 0.77。
   核多不等于快，8 是这台机器的甜点，所以写成可配的默认值而不是 `cpu_count()`。
3. **连贯性排在第一**：发声有两种停顿，一种是人话里本来就有的，另一种是
   流水线自己加的。后者实测有两处，都很贵：
   * `sd.OutputStream` 开一次关一次 **0.34 秒**（首次 0.95 秒）。逐句重开流，
     每个句号后面就白送半秒死气。
   * `stream.write()` 是**按实时走**的（写 1 秒音频阻塞 1.03 秒）。所以
     "合成一句 → 播一句 → 再合成下一句"这种串行写法里，**下一句的合成耗时
     （实测 2.0 秒）也全落在停顿里**。
   两样加起来，一段两句的回话中间能空出 2 秒多——听感就是"连贯性差"。
   所以现在的做法是：**整段只开一条流**，并且让合成线程**预取下一句**，
   播放和合成重叠。边界上只剩模型自己那个自然的句末停顿。

打断（barge-in）
----------------
`stop()` 必须立刻生效：置事件、abort 输出流、清空待播队列。用户按 Fn 时要能
一句话把它掐掉——这是"对话"，不是"广播"。
"""
from __future__ import annotations

import collections
import contextlib
import os
import queue
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np
import sounddevice as sd


class SpeakError(RuntimeError):
    """发声链路上任何可预期的失败（模型缺失、加载失败、音频设备打不开）。"""


# 按句切的标点。中英都要——用户的话和 Codex 的回复都是混着来的。
_SENTENCE_END = "。！？!?；;\n"
# 一句最长多少字。太长一句合成会让"边生成边播"失去意义，遇到逗号也要断开。
_MAX_SENTENCE_CHARS = 60
_SOFT_BREAK = "，,、）)】」"


def split_sentences(text: str) -> List[str]:
    """把一段话切成适合逐句合成的片段。"""
    out: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENTENCE_END or (ch in _SOFT_BREAK and len(buf) >= _MAX_SENTENCE_CHARS):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        elif len(buf) >= _MAX_SENTENCE_CHARS * 2:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def strip_markdown(text: str) -> str:
    """把不该念出来的东西去掉：代码块、行内代码、链接地址、强调符号。

    念 `**加粗**` 和 `https://...` 给耳朵听毫无意义，还会让合成器读出一串
    符号名。这里只做保守清理——**不改写内容**，只去掉格式记号。
    """
    import re

    text = re.sub(r"```.*?```", " （代码块略） ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)     # markdown 链接
    text = re.sub(r"https?://\S+", " 链接 ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # 标题记号
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.M)    # 列表记号
    text = re.sub(r"[*_~>|]", "", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


@contextlib.contextmanager
def _native_stderr_silenced():
    """临时把 fd 2 指向空设备。

    sherpa-onnx 的 C++ 在合成时会往 fd 2 直接写一句
    `Unknown token: ...`（词典里有个映射不上的符号，实测无害）。它绕开
    Python 的 sys.stderr，所以只能在这一层堵。不堵的话，每合成一句就往
    app.log 里塞一行噪音，几天就把真正有用的日志淹了。

    只在合成那一小段窗口内切换，且**一定还原**。
    """
    try:
        saved = os.dup(2)
    except OSError:
        yield
        return
    devnull = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        yield
    finally:
        try:
            os.dup2(saved, 2)
        finally:
            os.close(saved)
            if devnull is not None:
                os.close(devnull)


class Speaker:
    """常驻实例的发声能力。线程安全；`speak()` 立即返回，不阻塞调用方。"""

    def __init__(self, settings_getter: Callable[[], object],
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._settings = settings_getter
        self._log = log or (lambda _msg: None)
        self._lock = threading.Lock()
        self._tts = None
        self._model_dir = ""
        self._last_use = 0.0
        self._queue: "collections.deque[str]" = collections.deque()
        self._interrupt = threading.Event()
        # sherpa-onnx 的 generate() 不做并发保证，所以同一时刻只许一条线程合成。
        # 被打断的那条合成线程可能还没退干净，下一句就来了。
        self._synth_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stream = None
        self._speaking = False

    # ---------- 对外 ----------

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def model_loaded(self) -> bool:
        return self._tts is not None

    def speak(self, text: str) -> str:
        """把 text 排进待播队列，立刻返回**实际会念的**文本。

        返回实际文本是为了让调用方（插件）能如实说"我念了哪些"，而不是
        自己以为念了全部。
        """
        st = self._settings()
        clean = strip_markdown(text or "")
        clean = clean[:max(0, int(st.speak_max_chars))]
        if not clean.strip():
            return ""
        with self._lock:
            self._interrupt.clear()
            self._queue.append(clean)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="voicepill-speak", daemon=True)
                self._worker.start()
        return clean

    def stop(self) -> None:
        """立刻闭嘴。按 Fn 打断、或用户改主意时调。可从任意线程调。"""
        with self._lock:
            self._interrupt.set()
            self._queue.clear()
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()          # 关键：abort 而不是 stop，立刻断
            except Exception:           # noqa: BLE001 —— 设备已关就算了
                pass

    def maybe_unload(self) -> bool:
        """静置够久就把模型从内存里放掉。由主循环定期调。"""
        st = self._settings()
        idle = float(st.tts_idle_unload_seconds)
        if idle <= 0 or self._tts is None:
            return False
        with self._lock:
            if self._speaking or self._queue:
                return False
            if time.monotonic() - self._last_use < idle:
                return False
            self._tts = None
            self._model_dir = ""
        self._log("TTS 模型静置 %.0f 秒，已从内存释放" % idle)
        return True

    # ---------- 内部 ----------

    def _ensure_model(self):
        if self._tts is not None:
            return self._tts
        import sherpa_onnx

        st = self._settings()
        root = config_tts_dir()
        need = ["model.onnx", "voices.bin", "tokens.txt", "espeak-ng-data"]
        missing = [n for n in need if not os.path.exists(os.path.join(root, n))]
        if missing:
            raise SpeakError(
                "TTS 模型不完整（%s 缺 %s）。模型要放在 %s"
                % (root, "、".join(missing), root))
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=os.path.join(root, "model.onnx"),
                    voices=os.path.join(root, "voices.bin"),
                    tokens=os.path.join(root, "tokens.txt"),
                    data_dir=os.path.join(root, "espeak-ng-data"),
                    lexicon=",".join([
                        os.path.join(root, "lexicon-us-en.txt"),
                        os.path.join(root, "lexicon-zh.txt"),
                    ]),
                ),
                num_threads=max(1, int(st.tts_threads)),
            ),
        )
        if not cfg.validate():
            raise SpeakError("TTS 配置校验不过（模型目录：%s）" % root)
        t0 = time.time()
        # 加载时也会打那句无害的 Unknown token 警告，一起堵掉。
        with _native_stderr_silenced():
            tts = sherpa_onnx.OfflineTts(cfg)
        self._tts = tts
        self._model_dir = root
        self._log("TTS 模型加载完成（%.1fs，%d 音色，%s）"
                  % (time.time() - t0, tts.num_speakers, root))
        return tts

    def _run(self) -> None:
        self._speaking = True
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    text = self._queue.popleft()
                if self._interrupt.is_set():
                    return
                self._speak_one(text)
        except Exception as exc:        # noqa: BLE001 —— 说话失败不该拖垮常驻进程
            self._log("发声失败：%r" % (exc,))
        finally:
            self._speaking = False
            self._last_use = time.monotonic()

    def _speak_one(self, text: str) -> None:
        """念完一段话：**整段只开一条输出流**，且合成永远跑在播放前面一句。

        为什么不逐句"合成一句、播一句"（那是第一版，听感就是"不连贯"）：
        逐句重开流要付 0.34 秒/句的设备开关，串行合成又要把下一句的合成时间
        （实测 2.0 秒）全算进停顿。两个都是流水线自己加的，不是人话里的停顿。
        现在合成线程先把下一句备好，主线程只管往同一条流里写。

        预取位只有 1 个：够把合成和播放重叠起来，又不会在用户改主意时
        提前把一大段都合成出来（合成结果没法"取消"，只能不播）。
        """
        sentences = split_sentences(text)
        if not sentences:
            return
        ready: "queue.Queue" = queue.Queue(maxsize=1)
        done = object()
        failed: List[BaseException] = []

        def put(item) -> bool:
            """塞进预取位；但要能被 stop() 叫停，否则线程会卡在这上面。"""
            while not self._interrupt.is_set():
                try:
                    ready.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    continue
            return False

        def producer() -> None:
            try:
                for sentence in sentences:
                    if self._interrupt.is_set():
                        break
                    pair = self._synth(sentence)
                    if not put(pair):
                        break
            except BaseException as exc:     # noqa: BLE001 —— 带回主线程去报
                failed.append(exc)
            finally:
                # 哨兵必须**尽力**送到，但不能为此死等：消费者可能已经走了。
                try:
                    ready.put(done, timeout=2.0)
                except queue.Full:
                    pass

        th = threading.Thread(target=producer, name="voicepill-speak-synth",
                              daemon=True)
        th.start()
        stream = None
        rate = 0
        try:
            while True:
                try:
                    item = ready.get(timeout=0.2)
                except queue.Empty:
                    # 不靠哨兵兜底：合成线程要是卡在一次长合成里，哨兵会迟到。
                    # 被打断时 0.2 秒内自己收工，这是"按 Fn 立刻闭嘴"的一部分。
                    if self._interrupt.is_set():
                        break
                    continue
                if item is done:
                    break
                if self._interrupt.is_set():
                    break
                samples, chunk_rate = item
                if stream is None or chunk_rate != rate:
                    if stream is not None:          # 换采样率了：宁可重开也不变速
                        _quiet_close(stream)
                    rate = chunk_rate
                    stream = self._open_stream(rate)
                    stream.start()
                    with self._lock:
                        self._stream = stream
                if self._interrupt.is_set():
                    break
                try:
                    stream.write(samples)
                except Exception:                # noqa: BLE001
                    if self._interrupt.is_set():
                        break                    # 正常打断，不是故障
                    raise
        finally:
            with self._lock:
                self._stream = None
            if stream is not None:
                _quiet_close(stream)
            # 只等一小会儿：合成线程是 daemon，且 _synth_lock 保证它不会和
            # 下一次发声撞车，所以没必要为了"收干净"让按 Fn 的人多等一秒。
            th.join(0.3)
        if failed:
            raise failed[0]

    def _synth(self, sentence: str) -> Tuple["np.ndarray", int]:
        st = self._settings()
        tts = self._ensure_model()
        with self._synth_lock, _native_stderr_silenced():
            audio = tts.generate(text=sentence,
                                 sid=int(st.tts_speaker),
                                 speed=float(st.tts_speed))
        return np.asarray(audio.samples, dtype="float32"), int(audio.sample_rate)

    def _open_stream(self, rate: int):
        """开一条输出流。单独一个方法，是为了自测能把它打桩掉（不出声）。"""
        return sd.OutputStream(samplerate=rate, channels=1, dtype="float32")


def _quiet_close(stream) -> None:
    """关流，失败就算了——`stop()` 可能刚 abort 过它。"""
    try:
        stream.close()
    except Exception:                   # noqa: BLE001
        pass


def config_tts_dir() -> str:
    """独立的取目录函数：方便自测里打桩，也避免模块导入期就去读 settings。"""
    import config
    return config.tts_model_dir()