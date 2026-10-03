# -*- coding: utf-8 -*-
"""NDJSON 控制面通道自测：协议、鉴权、坏输入、七个命令。

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
        if not _k32.WaitNamedPipeW(name, int(connect_timeout * 1000)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = _k32.CreateFileW(name, _GENERIC_READ | _GENERIC_WRITE,
                                       0, None, _OPEN_EXISTING, 0, None)
        if not self.handle or self.handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())

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
            try:
                line = raw.read_line(5.0)
            except TimeoutError:
                line = b""
            if line:
                ck("超长行被拒（回了 ok:false）",
                   _json_line(line).get("ok") is False, line)
            else:
                ck("超长行被拒（连接被直接关掉）", True)
            ck("超长行没进 handler（写完整送出=%s）" % sent,
               not any(c.startswith("xxx") for c, _ in seen))
            ck("超长行之后这条被关掉", raw.read_line(3.0) == b"")

        with RawPipe(name) as raw:                    # 6) 收尾：好连接照旧
            _auth(raw)
            raw.write_line({"cmd": "status"})
            ck("五条坏输入之后，好连接照样能用",
               _json_line(raw.read_line(5.0)).get("ok") is True)
    finally:
        server.stop()


def check_concurrency(ck: Checker) -> None:
    """坏连接与好连接同时连：坏的不许影响好的"""
    seen: list = []
    server = _start(seen)
    name = bridge.json_pipe_name()
    good: dict = {}
    bad: dict = {}

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

    try:
        threads = [threading.Thread(target=bad_client),
                   threading.Thread(target=good_client)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(20)
        ck("错 auth 那条同时拿到了 ok:false",
           (bad.get("reply") or {}).get("ok") is False, bad)
        ck("好那条同时拿到了正常回复",
           (good.get("reply") or {}).get("ok") is True, good)
        ck("并发之后服务端还在", server.alive)
    finally:
        server.stop()


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
               check_bad_clients, check_concurrency, check_idle_timeout):
        print("\n[%s]" % title(fn))
        fn(ck)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
