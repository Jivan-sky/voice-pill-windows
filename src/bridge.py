# -*- coding: utf-8 -*-
"""本机控制面：让外部进程驱动驻留实例、取走转写文字。

为什么需要它
------------
驻留形态下这个进程对外只有两个口子：`--stop` 的一个命名事件，和
`settings.json`。都不够用——Codex 插件那边要能问「现在什么状态」、要能
让它录一次、要能把它刚转出来的文字取走。这条控制面就是补这一环。

为什么是命名管道而不是 TCP
--------------------------
命名管道不带网络栈：不会触发 Windows 防火墙弹窗，也不存在被别的机器连上
的可能。标准库 `multiprocessing.connection` 在 Windows 上就是命名管道，
顺带把 authkey 握手也给了，不必自己写 Win32 那一套。

契约
----
一次连接只处理一条请求，发完就关。请求与响应都是一行 JSON 对象：

    请求  {"cmd": "status"}
    请求  {"cmd": "start"}
    响应  {"ok": true,  "data": {...}}
    响应  {"ok": false, "error": "人话错误信息"}

命令只有七个：`status` / `start` / `stop` / `cancel` / `take` / `speak` /
`shutup`。命令集合刻意留小：这条通道每多一个动词，就多一份「同机进程能拿它
干什么」的想象力。`speak` / `shutup` 只让**本机扬声器**出声或闭嘴，读不到
任何数据、也不落盘（见 docs/移植方案.md 第 16 节）。

`take` 是**取走**语义（返回并清空），不是查看：改口供的场合比反复读同一段
多得多，而清空后队列里也不会一直堆着用户说过的话。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from multiprocessing.connection import (
    AuthenticationError,
    Client,
    Listener,
)
from typing import Any, Callable, Optional

import config

# 管道名前缀。**后面必须带用户标识**，不能所有用户共用一个名字。
#
# 用全局名的代价是实测出来的：本机另一个进程拿错密钥连上来（另一个用户的
# `%LOCALAPPDATA%`，或者密钥文件被删掉重建之后），握手两边都崩——
# 服务端的 `accept()` 抛 `AuthenticationError`（它不是 `OSError`，直接把
# accept 线程打死，**控制面就这么永久死了**），而客户端卡在 `recv` 上
# **永不返回**（实测 12 秒以上仍不回来）。名字带用户标识之后，这种客户端
# 连管道都找不到，直接 `OSError` 快速失败，两边都安全。
PIPE_PREFIX = r"\\.\pipe\VoicePill-"

# 自测用：整个换掉管道名，就能在不碰真身的前提下测「控制面不在」这条路。
PIPE_NAME_ENV = "VOICEPILL_PIPE_NAME"

# 语言无关的第二条通道（NDJSON）。跟旧通道同一个用户 tag，只换前缀。
JSON_PIPE_PREFIX = r"\\.\pipe\VoicePill-Json-"
JSON_PIPE_NAME_ENV = "VOICEPILL_JSON_PIPE_NAME"


def json_pipe_name() -> str:
    """NDJSON 通道的管道名。与 pipe_name() 同源，只有前缀不同。"""
    override = os.environ.get(JSON_PIPE_NAME_ENV, "").strip()
    if override:
        return override
    tag = hashlib.sha256(
        os.path.normcase(config.app_dir()).encode("utf-8")).hexdigest()[:16]
    return JSON_PIPE_PREFIX + tag


def auth_token(key: bytes, nonce: str) -> str:
    """挑战应答的应答：HMAC-SHA256(key, ASCII(nonce))，64 位小写 hex。"""
    import hmac as _hmac
    return _hmac.new(key, nonce.encode("ascii"), "sha256").hexdigest()
KEY_FILE = "bridge.key"

# 客户端「建连 + authkey 握手」的总上限（秒）。握手没有超时参数，只能自己卡。
CONNECT_TIMEOUT_SECONDS = 3.0

# 单条请求上限。控制面只收发小 JSON，超过这个量说明对端不是我们的客户端。
MAX_REQUEST_BYTES = 64 * 1024

# 一次连接最多活这么久（秒）。客户端用完就关，这里是防呆：万一有人连上不
# 说话，线程不会永远挂着。
CONNECTION_IDLE_SECONDS = 60.0

COMMANDS = ("status", "start", "stop", "cancel", "take",
            "speak", "shutup")


class BridgeError(Exception):
    """控制面不可用或对端返回了错误。"""


def pipe_name() -> str:
    """本用户专属的管道名。

    用户标识从 `config.app_dir()`（就是 `%LOCALAPPDATA%\\VoicePill`）推出来，
    不调 Win32：控制面两端读的是同一份环境变量，算出来必然一致。
    取哈希而不是把用户名直接拼进去——路径里可能有空格或非 ASCII，管道名
    不欢迎那些字符，哈希之后是纯十六进制。

    设了 `VOICEPILL_PIPE_NAME` 就整个覆盖（自测用，见 tools/bridge-selftest.py）。
    """
    override = os.environ.get(PIPE_NAME_ENV, "").strip()
    if override:
        return override
    tag = hashlib.sha256(
        os.path.normcase(config.app_dir()).encode("utf-8")).hexdigest()[:16]
    return PIPE_PREFIX + tag


def key_path() -> str:
    return os.path.join(config.app_dir(), KEY_FILE)


def load_or_create_key() -> bytes:
    """读写本机共享密钥。

    没密钥就没法连（`multiprocessing.connection` 会直接拒绝握手），所以两端
    都得能自己把它造出来——先起的造，后起的读。文件落在用户自己的
    `%LOCALAPPDATA%` 下，ACL 天然只对该用户开放。

    六十四位十六进制而不是二进制：文件是可读的，出问题时能一眼看出是不是被
    写坏了；密钥强度两边一样（32 字节）。
    """
    path = key_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if text:
            return bytes.fromhex(text)
    except (OSError, ValueError):
        pass          # 不存在或写坏了 → 重建，不抛

    key = secrets.token_bytes(32)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(key.hex())
    os.replace(tmp, path)
    return key


# ---------- 客户端（插件/脚本/自测都走这里）----------

def _connect(timeout: float = CONNECT_TIMEOUT_SECONDS):
    """建连 + authkey 握手，**带硬上限**。失败一律抛 `BridgeError`。

    `Client()` 的握手没有超时参数：密钥对不上时，服务端那边抛异常走人，
    客户端却永远等不到回话（实测 12 秒以上不返回，而且一直不返回）。所以
    把建连放进一个旁路线程，到点就放弃。

    代价说清楚：超时之后那个线程还挂在 `recv` 上（Python 杀不掉阻塞在系统
    调用里的线程），只有 daemon 线程随进程退出这一条兜底。调用方全是短命
    进程（钩子 / 自测 / `--check`），够用；**别在长命进程里反复吃到超时**。
    """
    box: dict = {}

    def worker() -> None:
        try:
            box["conn"] = Client(pipe_name(), family="AF_PIPE",
                                 authkey=load_or_create_key())
        except BaseException as exc:       # noqa: BLE001 —— 原样带走，外面分类
            box["error"] = exc

    th = threading.Thread(target=worker, name="voicepill-bridge-connect",
                          daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise BridgeError(
            "控制面握手 %.1f 秒没完成（密钥对不上，或控制面半死不活）。"
            % timeout)
    exc = box.get("error")
    if exc is not None:
        if isinstance(exc, AuthenticationError):
            raise BridgeError("控制面拒绝握手（authkey 对不上）：%s" % exc) from exc
        raise BridgeError("连不上控制面（常驻进程没在跑？）：%s" % exc) from exc
    return box["conn"]


def call(cmd: str, timeout: float = 15.0, **args: Any) -> Any:
    """发一条命令，返回 data。失败抛 BridgeError。

    每次调用新建连接：一次调用 = 一次连接，没有需要维护的长连接状态，也就
    没有「连接悄悄死了但没人发现」这种失败模式。管道建连开销在这个量级
    （一次录音几十秒）完全不算什么。
    """
    if cmd not in COMMANDS:
        raise BridgeError("未知命令：%s" % cmd)

    conn = _connect()

    try:
        conn.send({"cmd": cmd, "args": args})
        if not conn.poll(timeout):
            raise BridgeError("控制面 %s 秒没回话。" % timeout)
        reply = conn.recv()
    except (OSError, EOFError, ValueError) as exc:
        raise BridgeError("控制面通信中断：%s" % exc) from exc
    finally:
        try:
            conn.close()
        except OSError:
            pass

    if not isinstance(reply, dict):
        raise BridgeError("控制面回了个不认识的东西：%r" % (reply,))
    if not reply.get("ok"):
        raise BridgeError(reply.get("error") or "控制面拒绝了这条命令。")
    return reply.get("data")


def probe(timeout: float = 3.0) -> Optional[dict]:
    """尽力而为地问一次状态：连不上返回 None，不抛。给 `--check` 用。"""
    try:
        return call("status", timeout=timeout)
    except BridgeError:
        return None


# ---------- 服务端（跑在驻留进程里）----------

class BridgeServer:
    """命名管道服务端。内部起一个 accept 线程 + 每条连接一个处理线程。

    `handler(cmd, args)` 由调用方提供，返回 data；抛异常则原样变成 error
    文本回给对端（控制面不该因为一条坏命令把驻留进程带崩）。
    """

    def __init__(self, handler: Callable[[str, dict], Any]) -> None:
        self._handler = handler
        self._stop = threading.Event()
        self._listener: Optional[Listener] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.started = False
        self.error: Optional[str] = None
        # 最近一次「连上来但握手没过」的原因。这不是致命错误，只留个痕。
        self.last_error: Optional[str] = None

    def start(self) -> None:
        self._accept_thread = threading.Thread(
            target=self._serve, name="voicepill-bridge", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        """停止服务并唤醒阻塞在 accept() 上的线程。

        `Listener.accept()` 没有超时参数，光置事件叫不醒它。这里自己连一次
        自己，让 accept 立刻返回，循环下一圈看到事件就退出。
        """
        self._stop.set()
        try:
            _connect().close()
        except BridgeError:
            pass

    @property
    def alive(self) -> bool:
        t = self._accept_thread
        return bool(t and t.is_alive())

    # ---------- 内部 ----------

    def _serve(self) -> None:
        try:
            self._listener = Listener(
                pipe_name(), family="AF_PIPE", authkey=load_or_create_key())
        except OSError as exc:
            self.error = str(exc)
            return
        self.started = True

        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except OSError as exc:
                if not self._stop.is_set():
                    self.error = str(exc)
                break
            except Exception as exc:            # noqa: BLE001
                # **握手失败不是致命错误。** Listener 还活着，下一个正常客户端
                # 照样能连。以前这里只接 OSError，于是 `AuthenticationError`
                # 直接冒出去把 accept 线程带走——任何一个拿错密钥的本机进程都
                # 能让控制面**永久失联**，而驻留进程自己毫不知情。实测踩过。
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                continue
            threading.Thread(target=self._handle, args=(conn,),
                             name="voicepill-bridge-conn", daemon=True).start()

        try:
            self._listener.close()
        except OSError:
            pass

    def _handle(self, conn) -> None:
        try:
            if not conn.poll(CONNECTION_IDLE_SECONDS):
                return
            request = conn.recv()
            cmd, args = self._parse(request)
            data = self._handler(cmd, args)
            conn.send({"ok": True, "data": data})
        except Exception as exc:                      # noqa: BLE001
            # 任何异常都回给对端，绝不冒泡：这条线程死了不影响驻留进程，
            # 但对端该拿到人话，而不是干等超时。
            try:
                conn.send({"ok": False, "error": "%s: %s"
                           % (type(exc).__name__, exc)})
            except Exception:                         # noqa: BLE001
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _parse(request: Any) -> tuple:
        if not isinstance(request, dict):
            raise ValueError("请求不是 JSON 对象")
        cmd = request.get("cmd")
        if cmd not in COMMANDS:
            raise ValueError("未知命令：%r" % (cmd,))
        args = request.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("args 不是 JSON 对象")
        return cmd, args


def status_line() -> str:
    """给 `--check` 用的一行人话。"""
    data = probe()
    if data is None:
        return "连不上（驻留进程没在跑，或它没起来）"
    return "✅ 在听（pid %s，%s）" % (data.get("pid"), data.get("phase"))
