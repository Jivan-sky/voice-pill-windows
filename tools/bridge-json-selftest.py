# -*- coding: utf-8 -*-
"""NDJSON 控制面通道自测：协议、鉴权、坏输入、八个命令。

整轮跑在一条测试专用管道名（VOICEPILL_JSON_PIPE_NAME）上，不碰真身。

为什么需要它
------------
控制面要有第二张口子（NDJSON，语言无关，见 docs/移植方案.md 第 19 节）。
它是跨语言的契约：Go 侧 internal/bridge 必须用**同一组数字**算出同一条
管道名、同一个应答。所以这把尺子先盯两件最便宜、也最容易走散的事：

  * 管道名派生（新旧两条通道同源 tag、不同前缀、可被环境变量整个覆盖）；
  * 挑战应答（HMAC-SHA256 的金标准向量，Go 侧写同一条对拍）。

用法（任意 cwd 都行）：
    .venv\\Scripts\\python.exe tools\\bridge-json-selftest.py
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import bridge                      # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def title(fn) -> str:
    doc = (fn.__doc__ or fn.__name__).strip()
    return doc.splitlines()[0]


def check_pipe_name(ck: Checker) -> None:
    """管道名：另取前缀、同源 tag、可被环境变量覆盖"""
    name = bridge.json_pipe_name()
    ck("新通道有自己的前缀", name.startswith(r"\\.\pipe\VoicePill-Json-"), name)
    ck("与旧通道不是同一条", name != bridge.pipe_name())
    saved = os.environ.get(bridge.JSON_PIPE_NAME_ENV)
    os.environ[bridge.JSON_PIPE_NAME_ENV] = r"\\.\pipe\VoicePill-Json-selftest"
    try:
        ck("环境变量整个覆盖",
           bridge.json_pipe_name() == r"\\.\pipe\VoicePill-Json-selftest",
           bridge.json_pipe_name())
    finally:
        # 恢复现场要**还原**、不是删掉：main() 可能已经设好这一轮要用的名字，
        # 删掉等于把它一起抹了（tools/bridge-selftest.py 上踩过这个坑）。
        if saved is None:
            os.environ.pop(bridge.JSON_PIPE_NAME_ENV, None)
        else:
            os.environ[bridge.JSON_PIPE_NAME_ENV] = saved


def check_auth_token(ck: Checker) -> None:
    """挑战应答的金标准向量 —— Go 侧要用同一组数字对拍"""
    key = bytes(range(32))
    nonce = "0" * 32
    ck("HMAC-SHA256 金标准",
       bridge.auth_token(key, nonce) ==
       "9f8f7fbc3e6c1c6192a185bc2305596bb4681df6bb51f544f37c373679a5fe6f")
    ck("换个 nonce 就变",
       bridge.auth_token(key, "f" * 32) != bridge.auth_token(key, nonce))


# ---------- 测试专用的最小客户端（故意不复用 json_call）----------
#
# 服务端必须能自己站住，所以这一段不碰 bridge.py 里除常量之外的任何东西：
# 自己开管道、自己读行、自己判超时。ctypes 的 argtypes 一定要写全，否则
# 64 位句柄会被当成 32 位 int 传进去、高 32 位静默丢掉（然后就"连上了别的
# 管道"这种查不出来的事故）。

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PIPE_GONE = (6, 109, 232, 233)     # INVALID_HANDLE / BROKEN_PIPE / NO_DATA / NOT_CONNECTED

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class RawPipe:
    """一条裸连接：只做「写一行 / 读一行 / 关掉」。"""

    def __init__(self, name: str, connect_timeout: float = 3.0) -> None:
        # 同 bridge._open_pipe：服务端换实例的窗口里一条实例都没有，
        # WaitNamedPipeW 会立刻失败而不是等待，所以必须按截止时间重试。
        deadline = time.monotonic() + connect_timeout
        while True:
            remaining_ms = int(max(deadline - time.monotonic(), 0.0) * 1000)
            if remaining_ms and _k32.WaitNamedPipeW(name, remaining_ms):
                handle = _k32.CreateFileW(name,
                                          _GENERIC_READ | _GENERIC_WRITE,
                                          0, None, _OPEN_EXISTING, 0, None)
                if handle and handle != _INVALID_HANDLE:
                    self.handle = handle
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError("connect to %s timed out" % name)
            time.sleep(0.02)

    def write(self, data: bytes) -> bool:
        buf = ctypes.create_string_buffer(data, len(data))
        written = wintypes.DWORD(0)
        ok = _k32.WriteFile(self.handle, ctypes.cast(buf, ctypes.c_void_p),
                            len(data), ctypes.byref(written), None)
        return bool(ok) and written.value == len(data)

    def write_line(self, payload: dict) -> bool:
        line = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
        return self.write(line)

    def read_line(self, timeout: float) -> bytes:
        """读一行（含换行符）。对端关了返回空 bytes；超时抛 TimeoutError。"""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            available = wintypes.DWORD(0)
            if not _k32.PeekNamedPipe(self.handle, None, 0, None,
                                      ctypes.byref(available), None):
                if ctypes.get_last_error() in _PIPE_GONE:
                    return b""
                raise ctypes.WinError(ctypes.get_last_error())
            if not available.value:
                time.sleep(0.02)
                continue
            chunk = ctypes.create_string_buffer(min(available.value, 4096))
            got = wintypes.DWORD(0)
            if not _k32.ReadFile(self.handle, ctypes.cast(chunk, ctypes.c_void_p),
                                 len(chunk), ctypes.byref(got), None):
                return b""
            buf += chunk.raw[:got.value]
            if b"\n" in buf:
                return bytes(buf)
        raise TimeoutError("等了 %.1f 秒没等到一行" % timeout)

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _json_line(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def _auth(raw: RawPipe) -> str:
    """走完挑战应答，返回服务端给的 nonce（失败用例还要拿它做别的）。"""
    nonce = _json_line(raw.read_line(5.0))["nonce"]
    token = bridge.auth_token(bridge.load_or_create_key(), nonce)
    assert raw.write_line({"auth": token})
    return nonce


def _require_test_pipe() -> None:
    """起服务端之前，确认用的还是**测试专用**管道名。

    这条守的是安全：名字一旦退回真身那个，自测的服务端会因 FIRST_PIPE_INSTANCE
    建不起来，而自测的客户端会**连上真身**（take 会把用户的话取走、
    stop 会把真身停掉）。真身此刻可能正在跑，绝不许碰。
    """
    name = bridge.json_pipe_name()
    if "selftest" not in name:
        raise SystemExit(
            "自测中止：管道名是 %r，它会连到真身上。"
            "本自测必须在测试专用管道名下运行。" % name)


def _stub(seen: list):
    def handler(cmd: str, args: dict):
        seen.append((cmd, args or {}))
        return {"pid": 4321, "phase": "idle", "cmd": cmd, "echo": args or {}}
    return handler


def _start(seen: list):
    _require_test_pipe()
    server = bridge.JsonBridgeServer(_stub(seen))
    server.start()
    return server


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_ERROR_PIPE_CONNECTED = 535
_LF = bytes((10,))


def _read_at_least_lines(handle, wanted: int, timeout: float) -> bytes:
    """读到出现 wanted 个换行为止，返回读到的**全部**字节（可能多带后面的行）。

    不能按「一行」收：客户端把 auth 与请求连着写出来，一次 Peek/Read 就把两行
    一起拿到了（实测踩过——按一行收会把请求那行留在第一次的返回里，服务端于是
    永远等不到第二行）。
    """
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        available = wintypes.DWORD(0)
        if not _k32.PeekNamedPipe(handle, None, 0, None,
                                  ctypes.byref(available), None):
            if ctypes.get_last_error() in _PIPE_GONE:
                break
            raise ctypes.WinError(ctypes.get_last_error())
        if not available.value:
            time.sleep(0.02)
            continue
        chunk = ctypes.create_string_buffer(min(available.value, 4096))
        got = wintypes.DWORD(0)
        if not _k32.ReadFile(handle, ctypes.cast(chunk, ctypes.c_void_p),
                             len(chunk), ctypes.byref(got), None):
            break
        buf += chunk.raw[:got.value]
        if buf.count(_LF) >= wanted:
            return bytes(buf)
    if buf:
        return bytes(buf)
    raise TimeoutError("等了 %.1f 秒没读到 %d 行" % (timeout, wanted))


class RawServer:
    """裸服务端：握手、收两行、回一行，**把客户端写来的原始行原样留下**。

    桩 handler 拿到的是解析后的 dict，看不到编码，所以「客户端到底往管道里写了
    什么字节」只能在这一层看。
    """

    def __init__(self, name: str, reply: dict, nonce: str = "0" * 32) -> None:
        self.name = name
        self.reply = reply
        self.nonce = nonce
        self.raw = b""                     # 客户端写来的全部原始字节
        self.lines: list = []              # 按换行切开、去掉空段
        self.error = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        handle = 0
        try:
            handle = _k32.CreateNamedPipeW(
                self.name, _PIPE_ACCESS_DUPLEX,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
                _PIPE_UNLIMITED_INSTANCES,
                bridge.MAX_REQUEST_BYTES, bridge.MAX_REQUEST_BYTES, 0, None)
            if not handle or handle == _INVALID_HANDLE:
                raise ctypes.WinError(ctypes.get_last_error())
            connected = bool(_k32.ConnectNamedPipe(handle, None))
            err = ctypes.get_last_error()
            if not connected and err != _ERROR_PIPE_CONNECTED:
                raise ctypes.WinError(err)
            bridge._write_all(handle, bridge._encode_line({"nonce": self.nonce}))
            self.raw = _read_at_least_lines(handle, 2, 5.0)
            self.lines = [p for p in self.raw.split(_LF) if p]
            bridge._write_all(handle, bridge._encode_line(self.reply))
            _k32.FlushFileBuffers(handle)
            _k32.DisconnectNamedPipe(handle)
        except BaseException as exc:                  # noqa: BLE001
            self.error = repr(exc)
        finally:
            if handle:
                _k32.CloseHandle(handle)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)


def check_wire_bytes(ck: Checker) -> None:
    """发出去的原始字节：紧凑 JSON、中文原样 UTF-8、一行一个换行（计划第 255 行）"""
    _require_test_pipe()
    server = RawServer(bridge.json_pipe_name(), {"ok": True, "data": {}})
    server.start()
    try:
        data = bridge.json_call("speak", timeout=5.0, text="念这句", auto=False)
        ck("往返还是通的", data == {}, data)
        _wait(lambda: len(server.lines) >= 2 or server.error, 5)
        ck("服务端没报错", server.error is None, server.error)
        request = (server.lines[1] + _LF) if len(server.lines) > 1 else b""
        expected = ('{"cmd":"speak","args":{"text":"念这句","auto":false}}'
                    .encode("utf-8") + _LF)
        ck("请求行就是那串紧凑 UTF-8 字节", request == expected, request)
        ck("中文是原样 UTF-8，不是被转义成 ASCII",
           bool(request) and not request.isascii(), request)
        ck("键值之间没有多余空格（不是默认 separators）",
           bytes((34, 58, 32)) not in request, request)
        ck("行尾只有一个换行、没有 CR",
           request.endswith(_LF) and bytes((13,)) not in request, request)
    finally:
        server.join(10)


def check_roundtrip(ck: Checker) -> None:
    """正常路径：握手 -> status -> 拿到 handler 的返回"""
    seen: list = []
    server = _start(seen)
    try:
        ck("服务端起来了", _wait(lambda: server.started, 5),
           "error=%s" % server.error)
        with RawPipe(bridge.json_pipe_name()) as raw:
            nonce = _auth(raw)
            ck("nonce 是 32 位小写 hex",
               len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce),
               nonce)
            raw.write_line({"cmd": "status", "args": {"k": 1}})
            reply = _json_line(raw.read_line(5.0))
            ck("回的是 ok:true", reply.get("ok") is True, reply)
            ck("data 是 handler 的返回",
               (reply.get("data") or {}).get("pid") == 4321, reply)
            ck("args 原样到达 handler", seen[-1] == ("status", {"k": 1}), seen)
    finally:
        server.stop()
    ck("stop() 之后 accept 线程收了", _wait(lambda: not server.alive, 10))


def check_bad_clients(ck: Checker) -> None:
    """坏输入逐条：错密钥、非 JSON、未知命令、超长行、连上就跑"""
    seen: list = []
    ck("请求上限还是 64 KiB（没被人偷偷放大）",
       bridge.MAX_REQUEST_BYTES == 64 * 1024, bridge.MAX_REQUEST_BYTES)
    server = _start(seen)
    name = bridge.json_pipe_name()
    try:
        ck("服务端起来了", _wait(lambda: server.started, 5),
           "error=%s" % server.error)

        with RawPipe(name) as raw:                    # 1) 读到 nonce 就跑
            _json_line(raw.read_line(5.0))
        ck("不回 auth 直接断开：服务端还活着", server.alive)

        with RawPipe(name) as raw:                    # 2) 错 auth
            _json_line(raw.read_line(5.0))
            raw.write_line({"auth": "0" * 64})
            reply = _json_line(raw.read_line(5.0))
            ck("错 auth 回 ok:false", reply.get("ok") is False, reply)
            ck("错 auth 说了是鉴权", "鉴权" in str(reply.get("error")), reply)
            ck("错 auth 之后这条被关掉", raw.read_line(3.0) == b"")
        ck("错 auth 之后服务端还在", server.alive)
        ck("服务端记下了这次握手失败", bool(server.last_error), server.last_error)

        with RawPipe(name) as raw:                    # 3) 非 JSON
            _auth(raw)
            raw.write(b"this is not json\n")
            reply = _json_line(raw.read_line(5.0))
            ck("非 JSON 回 ok:false", reply.get("ok") is False, reply)
        ck("非 JSON 没被当命令执行", not any(c == "this is not json" for c, _ in seen))

        with RawPipe(name) as raw:                    # 4) 未知命令
            _auth(raw)
            raw.write_line({"cmd": "nope"})
            reply = _json_line(raw.read_line(5.0))
            ck("未知命令回 ok:false", reply.get("ok") is False, reply)
        ck("未知命令没进 handler", not any(c == "nope" for c, _ in seen))

        over = bridge.MAX_REQUEST_BYTES + 1           # 5) 超长行（比上限多 1 字节）
        with RawPipe(name) as raw:
            _auth(raw)
            sent = raw.write(b"x" * over)
            ck("超长行整条写完整送出了（不然「被拒」无从谈起）", sent, sent)
            try:
                line = raw.read_line(5.0)
            except TimeoutError:
                line = b""
            ck("超长行被拒：服务端给了明确回复（不是干等、也不是静默吞掉）",
               bool(line), line)
            reply = _json_line(line) if line else {}
            ck("超长行被拒：回的是 ok:false", reply.get("ok") is False, reply)
            ck("超长行被拒：说清了是超长",
               "超长" in str(reply.get("error")), reply)
            ck("超长行没进 handler（写完整送出=%s）" % sent,
               not any(c.startswith("xxx") for c, _ in seen))
            ck("超长行之后这条被关掉", raw.read_line(3.0) == b"")
        ck("超长行之后服务端还活着", server.alive)

        with RawPipe(name) as raw:                    # 6) 收尾：好连接照旧
            _auth(raw)
            raw.write_line({"cmd": "status"})
            ck("五条坏输入之后，好连接照样能用",
               _json_line(raw.read_line(5.0)).get("ok") is True)
    finally:
        server.stop()


def check_concurrency(ck: Checker) -> None:
    """坏连接与 8 条好连接一起打：坏的不许影响好的，好的也不许串线"""
    seen: list = []
    _require_test_pipe()
    server = bridge.JsonBridgeServer(_echo_stub(seen))
    server.start()
    name = bridge.json_pipe_name()
    good: dict = {}
    bad: dict = {}
    fan: dict = {}

    def good_client() -> None:
        try:
            with RawPipe(name, connect_timeout=5.0) as raw:
                _auth(raw)
                raw.write_line({"cmd": "status"})
                good["reply"] = _json_line(raw.read_line(5.0))
        except BaseException as exc:                  # noqa: BLE001
            good["error"] = repr(exc)

    def bad_client() -> None:
        try:
            with RawPipe(name, connect_timeout=5.0) as raw:
                _json_line(raw.read_line(5.0))
                raw.write_line({"auth": "0" * 64})
                bad["reply"] = _json_line(raw.read_line(5.0))
        except BaseException as exc:                  # noqa: BLE001
            bad["error"] = repr(exc)

    def fan_client(i: int) -> None:
        try:
            fan[i] = bridge.json_call("speak", timeout=15.0,
                                      text="第%d条" % i, auto=False)
        except BaseException as exc:                  # noqa: BLE001
            fan[i] = exc

    try:
        ck("服务端起来了", _wait(lambda: server.started, 5),
           "error=%s" % server.error)
        threads = [threading.Thread(target=bad_client),
                   threading.Thread(target=good_client)]
        threads += [threading.Thread(target=fan_client, args=(i,))
                    for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        ck("错 auth 那条同时拿到了 ok:false",
           (bad.get("reply") or {}).get("ok") is False, bad)
        ck("好那条同时拿到了正常回复",
           (good.get("reply") or {}).get("ok") is True, good)
        ck("8 条并发全都拿到了回包",
           len(fan) == 8 and all(isinstance(v, dict) for v in fan.values()),
           {k: repr(v) for k, v in fan.items()})
        ck("8 条并发各自拿到的就是自己那条",
           all(isinstance(fan.get(i), dict)
               and ((fan[i].get("got")) or {}).get("text") == "第%d条" % i
               for i in range(8)),
           {k: repr(v) for k, v in fan.items()})
        ck("并发之后服务端还在", server.alive)
    finally:
        server.stop()


_EIGHT = ("status", "start", "stop", "cancel", "take", "speak", "shutup",
          "capture")


def _echo_stub(seen: list):
    """桩 handler：命令与参数原样回显；take 给一份固定的「取走」结果。

    桩不碰密钥、不发网络：这把尺子量的是**通道**，不是引擎业务。
    """
    def handler(cmd: str, args: dict):
        seen.append((cmd, dict(args or {})))
        if (args or {}).get("boom"):
            raise ValueError("桩故意拒绝这条")
        if cmd == "take":
            return {"texts": ["一句话", "两句"], "count": 2}
        return {"echo": cmd, "got": dict(args or {})}
    return handler


def check_commands(ck: Checker) -> None:
    """八命令：回包对得上、参数原样到达、错误映射到 BridgeError"""
    seen: list = []
    _require_test_pipe()
    server = bridge.JsonBridgeServer(_echo_stub(seen))
    server.start()
    try:
        ck("服务端起来了", _wait(lambda: server.started, 5),
           "error=%s" % server.error)
        for cmd in _EIGHT:
            data = bridge.json_call(cmd, timeout=5.0)
            if cmd == "take":
                ck("take 往返：取走语义的返回原样带回",
                   data == {"texts": ["一句话", "两句"], "count": 2}, data)
            else:
                ck("%s 往返：data.echo 对得上" % cmd,
                   (data or {}).get("echo") == cmd, data)
        ck("八个命令一个不少、顺序也对",
           [c for c, _ in seen] == list(_EIGHT), [c for c, _ in seen])

        data = bridge.json_call("speak", timeout=5.0, text="念这句", auto=False)
        ck("speak：text 原样到达 handler",
           seen[-1] == ("speak", {"text": "念这句", "auto": False}), seen[-1])
        ck("speak：auto=False 原样到达（Stop 钩子与插件工具的分岔点）",
           seen[-1][1].get("auto") is False, seen[-1][1])
        ck("speak：返回里能看到 text",
           ((data or {}).get("got") or {}).get("text") == "念这句", data)

        try:
            bridge.json_call("speak", timeout=5.0, boom=True)
            ck("handler 抛异常 -> BridgeError", False, "没抛")
        except bridge.BridgeError as exc:
            ck("handler 抛异常 -> BridgeError",
               "桩故意拒绝" in str(exc), str(exc))
        ck("被拒的那条也真的进过 handler",
           seen[-1][1].get("boom") is True)

        try:
            bridge.json_call("nope", timeout=5.0)
            ck("未知命令在本侧就被拒", False, "没抛")
        except bridge.BridgeError as exc:
            ck("未知命令在本侧就被拒", "未知命令" in str(exc), str(exc))
        ck("未知命令没写进管道（handler 没见过）",
           not any(c == "nope" for c, _ in seen))
    finally:
        server.stop()


def check_client_oversize(ck: Checker) -> None:
    """单条请求超过 64 KiB：在本侧就报错，不写进管道"""
    seen: list = []
    _require_test_pipe()
    server = bridge.JsonBridgeServer(_echo_stub(seen))
    server.start()
    try:
        ck("服务端起来了", _wait(lambda: server.started, 5),
           "error=%s" % server.error)
        text = "x" * (bridge.MAX_REQUEST_BYTES + 100)
        started = time.monotonic()
        try:
            bridge.json_call("speak", timeout=5.0, text=text)
            ck("超长请求被本侧拒", False, "没抛")
        except bridge.BridgeError as exc:
            ck("超长请求被本侧拒（BridgeError）", True, str(exc))
        ck("在本侧就报错，没等超时",
           time.monotonic() - started < 3.0)
        ck("超长请求没写进管道（handler 没被叫）", not seen, seen)
        ck("服务端还活着", server.alive)
    finally:
        server.stop()


def check_client_timeout(ck: Checker) -> None:
    """收下请求但不回话：json_call 必须在 timeout 附近抛错，不许挂住"""
    ck("客户端建连上限还是 3.0 秒（没被人偷偷放大）",
       bridge.CONNECT_TIMEOUT_SECONDS == 3.0, bridge.CONNECT_TIMEOUT_SECONDS)
    saved = os.environ.get(bridge.JSON_PIPE_NAME_ENV)
    os.environ[bridge.JSON_PIPE_NAME_ENV] = \
        r"\\.\pipe\VoicePill-Json-selftest-slow-%d" % os.getpid()
    try:
        def slow(cmd: str, args: dict):
            time.sleep(30.0)
            return {"pid": 0}

        server = bridge.JsonBridgeServer(slow)
        server.start()
        try:
            ck("服务端起来了", _wait(lambda: server.started, 5),
               "error=%s" % server.error)
            started = time.monotonic()
            try:
                bridge.json_call("status", timeout=1.0)
                ck("不回话时 json_call 抛 BridgeError", False, "没抛")
            except bridge.BridgeError as exc:
                ck("不回话时 json_call 抛 BridgeError", True, str(exc))
            elapsed = time.monotonic() - started
            # 上界必须**小于** json_call 里 join 的兜底（timeout + 2.0 = 3.0 秒）：
            # 否则「读截止时间真的生效」和「只剩 join 兜底」都落在窗口里——
            # 把读截止时间改成 30 秒，这条照样全绿（Mendel 实测过）。
            ck("是在 1 秒那一档放弃的（0.9~2.0 秒，上界卡在 join 兜底之下）",
               0.9 <= elapsed <= 2.0, "%.2fs" % elapsed)
            ck("放弃之后服务端还活着", server.alive)
        finally:
            server.stop()
    finally:
        if saved is None:
            os.environ.pop(bridge.JSON_PIPE_NAME_ENV, None)
        else:
            os.environ[bridge.JSON_PIPE_NAME_ENV] = saved


def check_engine_absent(ck: Checker) -> None:
    """引擎不在：管道名指向一条没人听的管道 -> 快速抛 BridgeError"""
    saved = os.environ.get(bridge.JSON_PIPE_NAME_ENV)
    os.environ[bridge.JSON_PIPE_NAME_ENV] = \
        r"\\.\pipe\VoicePill-Json-selftest-absent-%d" % os.getpid()
    try:
        ck("用的是没人听的管道名",
           "selftest" in bridge.json_pipe_name())
        started = time.monotonic()
        try:
            # 故意**不传** timeout：顺带走一遍 json_call 的默认值
            # （CONNECT_TIMEOUT_SECONDS）。在此之前整份自测没有一处走过它。
            bridge.json_call("status")
            ck("没人听的时候 json_call 抛 BridgeError", False, "没抛")
        except bridge.BridgeError as exc:
            ck("没人听的时候 json_call 抛 BridgeError", True, str(exc))
        elapsed = time.monotonic() - started
        # 上界同样卡在 join 兜底（3.0 + 2.0 = 5.0 秒）之下；下界贴着默认值。
        ck("按默认的 3 秒收手（2.5~4.0 秒，不是永挂）",
           2.5 <= elapsed <= 4.0, "%.2fs" % elapsed)
    finally:
        if saved is None:
            os.environ.pop(bridge.JSON_PIPE_NAME_ENV, None)
        else:
            os.environ[bridge.JSON_PIPE_NAME_ENV] = saved


def check_idle_timeout(ck: Checker) -> None:
    """连上不说话：到点被服务端关掉（这条真要等满 60 秒，故意不缩短）"""
    ck("空闲上限还是 60 秒（没被人偷偷改小）",
       bridge.CONNECTION_IDLE_SECONDS == 60.0, bridge.CONNECTION_IDLE_SECONDS)
    seen: list = []
    server = _start(seen)
    try:
        started_at = time.monotonic()
        with RawPipe(bridge.json_pipe_name()) as raw:
            _auth(raw)
            try:
                closed = raw.read_line(70.0) == b""
            except TimeoutError:
                closed = False
        elapsed = time.monotonic() - started_at
        ck("闲着的连接被服务端关掉", closed)
        ck("是在 60 秒那一档关的（55~70 秒）", 55.0 <= elapsed <= 70.0,
           "%.1fs" % elapsed)
    finally:
        server.stop()

def main() -> int:
    # 整轮自测跑在一条测试专用管道上：真身此刻可能正在跑，绝不碰它的控制面。
    os.environ[bridge.JSON_PIPE_NAME_ENV] = \
        r"\\.\pipe\VoicePill-Json-selftest-%d" % os.getpid()
    _require_test_pipe()
    ck = Checker()
    print("=== NDJSON 控制面通道自测 ===")
    print("本次用的管道名：%s" % bridge.json_pipe_name())
    for fn in (check_pipe_name, check_auth_token, check_roundtrip,
               check_wire_bytes,
               check_bad_clients, check_commands, check_client_oversize,
               check_client_timeout, check_engine_absent, check_concurrency,
               check_idle_timeout):
        print("\n[%s]" % title(fn))
        fn(ck)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
