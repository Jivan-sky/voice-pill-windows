# -*- coding: utf-8 -*-
"""流式转写会话：子进程管道 + NDJSON 解码 + 背压。

对齐原版 macOS 的 Sources/LiveTranscriptionSession.swift 与 LiveProtocol.swift。

要点（照搬原版语义，理由见注释）：
  * stdout 必须用「短读」——拿当前可用字节，不是等满请求字节数。
    原版为此刻意绕开 FileHandle.read(upToCount:)，改用 POSIX read。
    Python 侧同理：用 os.read(fd, n)，它返回当前可用量。
  * 写入排队有上限，超限判定「卡死」，终止子进程但保留录音供重试。
  * **写入不可以在音频回调线程里做**。管道的 write 在子进程不读时会阻塞，
    而调用方是 PortAudio 的音频线程——一阻塞就直接掉帧、爆音。
    所以 append() 只记账 + 入队，真正的 write 在独立写线程里跑。
    这样背压上限才真的有意义（否则管道缓冲先满，上限永远轮不到触发）。
  * 成功判定 = exit code 0 **且** 缓冲无残留 **且** 拿到完成事件文本非空。
  * 事件 text 是整句快照，直接覆盖，不做拼接。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Callable, Optional

from providers import Provider, get as get_provider

# 原版 LiveTranscriptionSession.swift:62 —— 排队音频上限，超了判定卡死
MAX_PENDING_BYTES = 262_144
# 原版 LiveProtocol.swift:25 —— 单条事件上限
MAX_EVENT_BYTES = 1_048_576
# 失败时截取 stderr 尾部长度（原版 suffix(1600)）
STDERR_TAIL = 1600


class LiveStreamError(Exception):
    pass


class LiveEventDecoder:
    """按行切分 NDJSON，逐条产出整句快照。对齐 LiveProtocol.swift:19-45。"""

    def __init__(self, provider: Provider):
        self.provider = provider
        self._pending = bytearray()
        self.final_text: Optional[str] = None

    def append(self, data: bytes) -> list[str]:
        self._pending.extend(data)
        if len(self._pending) > MAX_EVENT_BYTES:
            raise LiveStreamError("转写事件过大（超过 1 MiB）")

        updates: list[str] = []
        while True:
            idx = self._pending.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._pending[:idx])
            del self._pending[: idx + 1]
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except UnicodeDecodeError as exc:
                # 编码错和 JSON 语法错是两回事，报错要能一眼分开——否则照着
                # "不是合法 JSON"去查语法，查一天也查不到是 GBK 在捣鬼。
                raise LiveStreamError(
                    "引擎 stdout 不是 UTF-8（%s）。原始字节：%r"
                    % (exc, bytes(line[:120]))) from exc
            except ValueError as exc:
                raise LiveStreamError("转写事件不是合法 JSON: %s" % exc) from exc

            etype = event.get("type")
            if etype == "error":
                raise LiveStreamError("转写连接失败，录音已留存可重试。")
            if etype not in ("partial", "final", "result"):
                continue
            text = event.get("text")
            if not isinstance(text, str):
                continue
            if etype == self.provider.completion_event:
                self.final_text = text
            updates.append(text)
        return updates

    def completed(self, exit_status: int) -> str:
        """对齐 LiveProtocol.swift:39-44 的三重判定。"""
        text = (self.final_text or "").strip()
        if exit_status != 0 or self._pending or not text:
            raise LiveStreamError("未收到完整转写结果，录音已留存可重试。")
        return text


class LiveTranscriptionSession:
    """一次流式转写会话。"""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._log_path: Optional[str] = None
        self._log_file = None
        self._lock = threading.Lock()
        self._pending_bytes = 0
        self._accepting = True
        self._completed = False
        self._stdin_closed = False
        # 写线程的输入队列。字节数由 _pending_bytes 单独记账，队列本身不设上限。
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._on_update: Callable[[str], None] = lambda _t: None
        self._on_complete: Callable[[Optional[str], Optional[str]], None] = \
            lambda _t, _e: None

    # ---------- 生命周期 ----------

    def start(self, binary_path: str, provider: Provider, log_path: str,
              punctuation: bool,
              on_update: Callable[[str], None],
              on_complete: Callable[[Optional[str], Optional[str]], None]) -> None:
        """启动子进程。on_complete(文本, 错误信息) 恰好被调用一次。"""
        self._on_update = on_update
        self._on_complete = on_complete

        if not os.path.isfile(binary_path):
            self._finish_once(None, "找不到转写引擎：%s" % binary_path)
            return

        # 经 config.engine_argv 展开：.exe 原样，.py/.cmd 套解释器（自测用假引擎）
        import config
        args = config.engine_argv(binary_path) + provider.arguments(
            punctuation=punctuation)

        # stderr 落日志文件，失败时读尾部当错误详情（原版同）
        self._log_path = log_path
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        self._log_file = open(log_path, "wb")

        # stdout 的编码契约是 **UTF-8**。Rust/Go 引擎天然如此，不受影响；
        # 但 Python 写的引擎（含自测用的 tools/mock-asr.py）一旦 stdout 被管道
        # 接走，就会退回 locale 编码 —— 本机 GBK，中文事件直接让解码器炸：
        #   'utf-8' codec can't decode byte 0xbc in position 29
        # 与其要求每个引擎各自记得兜，不如在这里把子解释器钉死。幂等、无害。
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log_file,
                bufsize=0,          # 无缓冲，短事件立刻可读
                env=env,
            )
        except OSError as exc:
            self._finish_once(None, "无法启动转写引擎：%s" % exc)
            return

        threading.Thread(target=self._read_loop, args=(provider,),
                         daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def append(self, data: bytes) -> None:
        """灌音频。**非阻塞**：只记账 + 入队，绝不碰管道。

        超过背压上限即判定卡死并终止会话——此时录音已经在 WAV 里，可重试。
        """
        if not data:
            return
        with self._lock:
            if not self._accepting:
                return
            if self._pending_bytes + len(data) > MAX_PENDING_BYTES:
                self._accepting = False
                stalled = True
            else:
                self._pending_bytes += len(data)
                stalled = False

        if stalled:
            self._terminate()
            self._finish_once(None, "转写停滞，录音继续留存可重试。")
            return

        self._queue.put(data)

    def finish(self) -> None:
        """松手时调用：让写线程灌完余量后关掉 stdin，引擎才会收尾出结果。"""
        with self._lock:
            self._accepting = False
            first = not self._stdin_closed
            self._stdin_closed = True
        if first:
            self._queue.put(None)       # 哨兵：写完队列里剩的再关 stdin

    def cancel(self) -> None:
        """Esc 取消：直接终止，不给结果。"""
        with self._lock:
            self._accepting = False
            self._completed = True
            self._stdin_closed = True
        self._queue.put(None)           # 让写线程退出，别僵在那儿
        self._terminate()
        self._cleanup_log()

    # ---------- 内部 ----------

    def _write_loop(self) -> None:
        """唯一的管道写入者。阻塞只会阻塞本线程，不会波及音频回调。"""
        while True:
            chunk = self._queue.get()
            if chunk is None:
                break
            try:
                proc = self._proc
                if proc and proc.stdin and proc.poll() is None:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass                      # 子进程已死/管道已断：丢弃即可
            finally:
                with self._lock:
                    self._pending_bytes -= len(chunk)

        # 队列排空后才关 stdin —— 关早了会把尾音切掉
        proc = self._proc
        if proc and proc.stdin:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _read_loop(self, provider: Provider) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        fd = proc.stdout.fileno()
        decoder = LiveEventDecoder(provider)

        try:
            while True:
                # os.read = 短读，返回当前可用字节（对齐原版 StreamPipe.swift）
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                for text in decoder.append(chunk):
                    self._on_update(text)
        except LiveStreamError as exc:
            self._terminate()
            self._finish_once(None, str(exc))
            return
        except OSError as exc:
            self._terminate()
            self._finish_once(None, "读取转写输出失败：%s" % exc)
            return

        try:
            proc.wait(timeout=max(provider.finish_timeout, 1.0))
        except subprocess.TimeoutExpired:
            self._terminate()
            self._finish_once(None, "转写引擎未在超时内结束，录音已留存。")
            return

        try:
            text = decoder.completed(proc.returncode)
        except LiveStreamError as exc:
            detail = self._read_log_tail()
            self._finish_once(None, detail or str(exc))
            return
        finally:
            self._cleanup_log()

        self._finish_once(text, None)

    def _read_log_tail(self) -> str:
        if not self._log_path or not os.path.isfile(self._log_path):
            return ""
        try:
            with open(self._log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - STDERR_TAIL))
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def _terminate(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _cleanup_log(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _finish_once(self, text: Optional[str], error: Optional[str]) -> None:
        """保证完成回调只触发一次。"""
        with self._lock:
            if self._completed:
                return
            self._completed = True
            self._accepting = False
        self.finish()
        self._cleanup_log()
        cb = self._on_complete
        threading.Thread(target=lambda: cb(text, error), daemon=True).start()
