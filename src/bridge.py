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

import ctypes
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from ctypes import wintypes
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
    return hmac.new(key, nonce.encode("ascii"), "sha256").hexdigest()
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


# ---------- 第二条通道：NDJSON 命名管道（语言无关）----------
#
# 上面那个 BridgeServer 走的是 multiprocessing.connection：挑战应答是 HMAC-MD5、
# 帧是 pickle，**只有 Python 说得出来**。控制面是给外部进程用的契约，不该被
# 实现语言钉死（issue #1 要的就是「Codex 之外的 Agent 也能挂上」），所以这里再
# 开一张嘴：同样七个命令、同一份 bridge.key，线上跑的是一行一条 JSON。旧的
# 通道一个字不改，纯增量。
#
# 为什么用 ctypes 直调 kernel32：标准库没有命名管道**服务端**，而这件事不值得
# 为它引入第三方包。每条连接都在自己的线程里处理：坏连接（错密钥、非 JSON、
# 超长行、连上不说话）只影响它自己，accept 循环照转——旧通道栽过的那个坑
# （一次握手失败把 accept 线程打死、控制面永久失联，见 docs/移植方案.md 12.5）
# 不许在这里重演。

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# argtypes/restype 一个都不能省：句柄是 64 位，不声明就会被当成 32 位 int 传，
# 高 32 位静默丢掉——然后就"连上了别的东西"，还查不出来。
_k32.CreateNamedPipeW.restype = wintypes.HANDLE
_k32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_UNLIMITED_INSTANCES = 255
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_GENERIC_READ_WRITE = 0xC0000000
_OPEN_EXISTING = 3

# ConnectNamedPipe 的两个「其实没事」的错误码
_ERROR_PIPE_CONNECTED = 535      # 客户端在 CreateNamedPipe 与 ConnectNamedPipe 之间连上了
_ERROR_NO_DATA = 232             # 那一瞬间连上又跑了

# PeekNamedPipe 只回答「有没有数据」，没有就歇一下再看。用轮询而不是阻塞
# ReadFile，是为了让"空闲 60 秒"这条规则能**准时**生效：阻塞中的同步
# ReadFile 关不掉（CloseHandle 不取消挂起的 I/O），而 CancelSynchronousIo
# 又要多引一层线程句柄。20 毫秒的粒度对控制面绰绰有余。
_READ_POLL_SECONDS = 0.02


class _PipeClosed(Exception):
    """对端关了管道（或者我们把这条自己的管道关了）。"""


class _LineTooLong(Exception):
    """一行超过 MAX_REQUEST_BYTES：对端不像我们的客户端。"""


def _close_handle(handle) -> None:
    try:
        _k32.CloseHandle(handle)
    except OSError:
        pass


def _winerror_text(where: str) -> str:
    return "%s 失败：WinError %d" % (where, ctypes.get_last_error())


def _parse_json_line(raw: bytes) -> dict:
    """把一行 UTF-8 JSON 解析成对象。说人话，别把栈丢给对端。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("这一行不是 JSON 对象（%s）" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("这一行不是 JSON 对象")
    return data


def _open_pipe(name: str, timeout_ms: int = 0):
    """尽力连一次命名管道；连不上返回 0（给 stop() 叫醒 accept 用）。"""
    if timeout_ms and not _k32.WaitNamedPipeW(name, timeout_ms):
        return 0
    handle = _k32.CreateFileW(name, _GENERIC_READ_WRITE, 0, None,
                              _OPEN_EXISTING, 0, None)
    if not handle or handle == _INVALID_HANDLE:
        return 0
    return handle


class _PipeReader:
    """一条连接上的行读取器：把「读完一行」与「剩下的字节」绑在一起。

    服务端每次 ReadFile 都可能一次带回两行——客户端常常把 auth 和请求连着写
    （自测里正是如此），只按第一个换行切、把多余的字节丢掉，第二条就永远等
    不到了。所以多余的字节必须留在**这条连接**上，下次接着用。
    """

    def __init__(self, handle) -> None:
        self._handle = handle
        self._buf = bytearray()

    def read_line(self) -> bytes:
        """读到换行符为止。对端关了抛 _PipeClosed；太长抛 _LineTooLong。"""
        while True:
            end = self._buf.find(b"\n")
            if end >= 0:
                line = bytes(self._buf[:end])
                del self._buf[:end + 1]
                return line
            if len(self._buf) > MAX_REQUEST_BYTES:
                raise _LineTooLong()
            available = wintypes.DWORD(0)
            if not _k32.PeekNamedPipe(self._handle, None, 0, None,
                                      ctypes.byref(available), None):
                raise _PipeClosed(_winerror_text("PeekNamedPipe"))
            if not available.value:
                time.sleep(_READ_POLL_SECONDS)
                continue
            chunk = ctypes.create_string_buffer(min(available.value, 4096))
            got = wintypes.DWORD(0)
            if not _k32.ReadFile(self._handle,
                                 ctypes.cast(chunk, ctypes.c_void_p),
                                 len(chunk), ctypes.byref(got), None):
                raise _PipeClosed(_winerror_text("ReadFile"))
            if not got.value:
                raise _PipeClosed("管道读回来 0 字节")
            self._buf += chunk.raw[:got.value]


class JsonBridgeServer:
    """NDJSON 命名管道服务端。对外形状与 BridgeServer 一致。

    `handler(cmd, args)` 由调用方提供，返回 data；抛异常则原样变成 error
    文本回给对端——控制面不该因为一条坏命令把驻留进程带崩。
    """

    def __init__(self, handler: Callable[[str, dict], Any]) -> None:
        self._handler = handler
        self._stop = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None
        self._name = json_pipe_name()
        self._key = b""
        self.started = False
        self.error: Optional[str] = None
        # 最近一次「连上来但没通过握手」的原因。不致命，只留个痕。
        self.last_error: Optional[str] = None

    def start(self) -> None:
        self._name = json_pipe_name()
        self._key = load_or_create_key()
        self._accept_thread = threading.Thread(
            target=self._serve, name="voicepill-json-bridge", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        """停服务，并叫醒阻塞在 ConnectNamedPipe 上的 accept 线程。

        它没有超时参数，光置事件叫不醒——自己连一次自己（照 BridgeServer.stop
        的做法）。连不上就算了，join 也只等一小会儿，绝不吊死调用方。
        """
        self._stop.set()
        handle = _open_pipe(self._name, timeout_ms=2000)
        if handle:
            _close_handle(handle)
        thread = self._accept_thread
        if thread is not None:
            thread.join(timeout=5.0)

    @property
    def alive(self) -> bool:
        thread = self._accept_thread
        return bool(thread and thread.is_alive())

    # ---------- 内部 ----------

    def _serve(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                handle = self._accept(first)
            except OSError as exc:
                if not self._stop.is_set():
                    self.error = str(exc)
                break
            first = False
            if handle is None:
                if self._stop.is_set():
                    break
                continue
            threading.Thread(target=self._handle, args=(handle,),
                             name="voicepill-json-conn", daemon=True).start()

    def _accept(self, first: bool):
        """建一个实例、等一条连接。没连上返回 None（下一圈接着等）。"""
        flags = _PIPE_ACCESS_DUPLEX
        if first:
            # 名字已经被占（真身在跑，或上一轮没退干净）就**直接失败**，绝不
            # 悄悄变成第二个实例去和真身抢客户端。
            flags |= _FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = _k32.CreateNamedPipeW(
            self._name, flags,
            0,       # PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT 三者都是 0
            _PIPE_UNLIMITED_INSTANCES,
            MAX_REQUEST_BYTES, MAX_REQUEST_BYTES,
            0, None)
        if not handle or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        self.started = True
        try:
            connected = bool(_k32.ConnectNamedPipe(handle, None))
            error_code = ctypes.get_last_error()
        except BaseException:
            _close_handle(handle)
            raise
        if self._stop.is_set():
            _close_handle(handle)
            return None
        if connected or error_code == _ERROR_PIPE_CONNECTED:
            return handle
        _close_handle(handle)
        if error_code != _ERROR_NO_DATA:
            time.sleep(0.05)     # 别在错误上空转烧 CPU
        return None

    def _handle(self, handle) -> None:
        """处理一条连接。任何异常只回一条 ok:false，然后关掉这一条。"""
        closed = threading.Event()

        def close_once() -> None:
            # 空闲计时器与正常收尾都会走这里：句柄只能关一次，双关会把别的
            # 连接带崩。
            if not closed.is_set():
                closed.set()
                _close_handle(handle)

        timer = threading.Timer(CONNECTION_IDLE_SECONDS, close_once)
        timer.daemon = True
        timer.start()
        try:
            self._serve_connection(handle)
        except _LineTooLong:
            self._try_write(handle, {
                "ok": False,
                "error": "请求超长（上限 %d 字节）" % MAX_REQUEST_BYTES})
        except _PipeClosed:
            pass
        except Exception as exc:                      # noqa: BLE001
            self._try_write(handle, {
                "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            timer.cancel()
            close_once()

    def _serve_connection(self, handle) -> None:
        reader = _PipeReader(handle)
        nonce = secrets.token_hex(16)
        self._write(handle, {"nonce": nonce})
        request = _parse_json_line(reader.read_line())
        token = request.get("auth")
        if not isinstance(token, str) or not hmac.compare_digest(
                auth_token(self._key, nonce), token):
            # 握手失败**不是**致命错误：只关这一条，accept 照转。
            self.last_error = "鉴权失败：对端没给出正确的应答"
            self._write(handle, {"ok": False, "error": "鉴权失败"})
            return
        request = _parse_json_line(reader.read_line())
        cmd, args = BridgeServer._parse(request)
        self._write(handle, {"ok": True, "data": self._handler(cmd, args)})

    def _write(self, handle, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > MAX_REQUEST_BYTES:
            raise ValueError("响应超过 %d 字节" % MAX_REQUEST_BYTES)
        buf = ctypes.create_string_buffer(line, len(line))
        written = wintypes.DWORD(0)
        if not _k32.WriteFile(handle, ctypes.cast(buf, ctypes.c_void_p),
                              len(line), ctypes.byref(written), None):
            raise _PipeClosed(_winerror_text("WriteFile"))

    def _try_write(self, handle, payload: dict) -> None:
        """错误路径上回话；回不出去就算了（对端可能已经走了）。"""
        try:
            self._write(handle, payload)
        except (OSError, _PipeClosed, ValueError):
            pass


def status_line() -> str:
    """给 `--check` 用的一行人话。"""
    data = probe()
    if data is None:
        return "连不上（驻留进程没在跑，或它没起来）"
    return "✅ 在听（pid %s，%s）" % (data.get("pid"), data.get("phase"))
