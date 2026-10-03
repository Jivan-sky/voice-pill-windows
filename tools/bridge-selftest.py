# -*- coding: utf-8 -*-
"""控制面（命名管道）自测：协议本身，以及「坏客户端能不能把它搞死」。

为什么需要它
------------
控制面是插件唯一的口子。它有两种坏法，都不出声、也不报错，但都很难查：

  * **管道名是全局的** —— 同一台机器上另一个用户（或密钥文件被删掉重建之后）
    拿错密钥连上来，握手两边都崩：服务端 `accept()` 抛 `AuthenticationError`
    （不是 `OSError`，直接把 accept 线程打死，控制面**永久失联**），客户端卡在
    `recv` 上**永不返回**。既没有日志、也没有异常，只有"插件忽然连不上了"。
  * **握手没有超时** —— 在上面那种情况下，调用方会一直挂着，而不是失败。

所以这个尺子盯的不是"正常路径能不能用"（那早就验过了），而是**坏输入**：
拿错密钥的客户端不许挂住、更不许把控制面带走。

全程在一条**测试专用的管道名**上跑（`VOICEPILL_PIPE_NAME`），不碰真身。

用法：
    .venv\\Scripts\\python.exe tools\\bridge-selftest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

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


def _require_test_pipe() -> None:
    """起服务端之前，确认用的还是**测试专用**管道名。

    这条守的是安全，不是洁癖。名字一旦退回真身那个：
      * `BridgeServer.start()` 会因为真身已经占着这个名字而报 WinError 5
        （`CreateNamedPipe` 带 `FILE_FLAG_FIRST_PIPE_INSTANCE`），
      * 而测试的**客户端**会**连上真身**——`stop()` 会去停真身，`take()` 会
        把用户的话取走。哪一边中招全看执行顺序。
    实测踩过：`check_pipe_name` 结尾为了恢复现场把环境变量整个删了，把
    `main()` 设好的名字一起带走，于是后面每一项都在拿真身的名字跑。
    """
    name = bridge.pipe_name()
    if "selftest" not in name:
        raise SystemExit(
            "自测中止：管道名是 %r，它会连到真身上。"
            "本自测必须在测试专用管道名下运行。" % name)


def _server(handler):
    """起一个测试专用服务端（先确认没在用真身的名字）。"""
    _require_test_pipe()
    server = bridge.BridgeServer(handler)
    server.start()
    return server


def check_pipe_name(ck: Checker) -> None:
    """管道名必须按用户派生，不能是一台机器共用的全局名。"""
    name = bridge.pipe_name()
    ck("默认名带用户标识（不是裸的多用户共用名）",
       name.startswith(bridge.PIPE_PREFIX) and len(name) > len(bridge.PIPE_PREFIX) + 8,
       name)
    ck("同一个用户算两次结果一致（控制面两端必须算出同一个名字）",
       bridge.pipe_name() == name)
    saved = os.environ.get(bridge.PIPE_NAME_ENV)
    os.environ[bridge.PIPE_NAME_ENV] = r"\\.\pipe\VoicePill-selftest-probe"
    try:
        ck("环境变量能整个换掉管道名（自测靠它不碰真身）",
           bridge.pipe_name() == r"\\.\pipe\VoicePill-selftest-probe",
           bridge.pipe_name())
    finally:
        # 恢复现场要**还原**，不是删掉：main() 早就设好了这一轮要用的测试
        # 管道名，删掉等于把它一起抹了（实测踩过，见 _require_test_pipe）。
        if saved is None:
            os.environ.pop(bridge.PIPE_NAME_ENV, None)
        else:
            os.environ[bridge.PIPE_NAME_ENV] = saved


def check_protocol(ck: Checker) -> None:
    """协议本身：往返、错误回给对端、未知命令被拒。"""
    seen = {}

    def handler(cmd: str, args: dict):
        seen["last"] = (cmd, args)
        if cmd == "status":
            # 异常路径只能这样测：`call()` 在客户端就把未知命令拦了，
            # 所以"让处理器炸"必须发生在**合法命令**的路径上。
            if args.get("boom"):
                raise RuntimeError("故意炸一个")
            return {"echo": args} if args else {"pid": 1234, "phase": "idle"}
        if cmd == "take":
            return [{"text": "测试", "ts": 1}]
        return {"echo": args}

    server = _server(handler)
    ck("服务端起来了", _wait(lambda: server.started, 10),
       "error=%s" % server.error)

    ck("status 往返", bridge.call("status") == {"pid": 1234, "phase": "idle"})
    ck("参数能带过去", bridge.call("status", x=1) == {"echo": {"x": 1}})

    ck("正常返回的 list 不会被当成错误",
       bridge.call("take") == [{"text": "测试", "ts": 1}])

    # 未知命令在客户端就被拦（不到服务端）
    try:
        bridge.call("rm -rf")
        ck("未知命令被拒", False, "居然发出去了")
    except bridge.BridgeError as exc:
        ck("未知命令被拒", "未知命令" in str(exc), str(exc))

    # 处理器抛异常 → 对端拿人话，而不是干等超时
    t0 = time.time()
    try:
        bridge.call("status", boom=True)
        ck("处理器抛异常时对端拿得到人话", False, "居然成功了")
    except bridge.BridgeError as exc:
        ck("处理器抛异常时对端拿得到人话", "故意炸一个" in str(exc), str(exc))
    ck("异常路径够快（不是等超时）", time.time() - t0 < 5.0,
       "%.2fs" % (time.time() - t0))

    server.stop()
    ck("stop() 之后 accept 线程收了", _wait(lambda: not server.alive, 10))
    try:
        bridge.call("status", timeout=2.0)
        ck("stop() 之后调用报错而不是静默", False, "居然连上了")
    except bridge.BridgeError:
        ck("stop() 之后调用报错而不是静默", True)


def check_bad_key(ck: Checker) -> None:
    """拿错密钥的客户端：不许挂住，更不许把控制面打死。

    复现方式就是真身会遇到的那种：**管道名一样，密钥不一样**（换一份
    `%LOCALAPPDATA%` 就会造出另一把密钥，密钥文件被删掉重建也一样）。
    """
    server = _server(lambda cmd, args: {"ok-here": True})
    ck("服务端起来了（第二批）", _wait(lambda: server.started, 10),
       "error=%s" % server.error)
    ck("正常客户端先能连上（基线）", _wait(lambda: _can_call(), 10))

    dead_appdata = tempfile.mkdtemp(prefix="voicepill-badkey-")
    env = dict(os.environ, LOCALAPPDATA=dead_appdata,
               VOICEPILL_PIPE_NAME=bridge.pipe_name(),
               PYTHONIOENCODING="utf-8")   # 子进程的 stdout 要走 UTF-8
    src = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src")
    # 子进程里必须换一份 LOCALAPPDATA：那会造出**另一把密钥**（现实里
    # "密钥文件被删掉重建"就是这个效果），而管道名仍然指着同一个服务端。
    code = (
        "import sys, time, json\n"
        "sys.path.insert(0, @SRC@)\n"
        "import bridge\n"
        "t0 = time.time()\n"
        "try:\n"
        "    bridge.call('status', timeout=2.0)\n"
        "    out = {'outcome': '连上了（不该发生）', 'dt': time.time() - t0}\n"
        "except bridge.BridgeError as exc:\n"
        "    out = {'outcome': str(exc)[:160], 'dt': time.time() - t0}\n"
        "except BaseException as exc:\n"
        "    out = {'outcome': 'RAW ' + type(exc).__name__ + ': ' + str(exc),\n"
        "           'dt': time.time() - t0}\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
    ).replace("@SRC@", repr(src))

    budget = bridge.CONNECT_TIMEOUT_SECONDS + 5.0
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, env=env)
    dt = time.time() - t0
    try:
        out = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"outcome": "没吐 JSON：%r / %r" % (proc.stdout, proc.stderr)}

    ck("错密钥的客户端返回了（不会一直挂着）", dt < budget,
       "耗时 %.1fs，上限 %.1fs" % (dt, budget))
    ck("错密钥是当 BridgeError 报出来的（调用方不必懂握手细节）",
       not str(out.get("outcome", "")).startswith("RAW"), out.get("outcome"))
    ck("报的错说清了是握手这一层",
       "握手" in str(out.get("outcome", "")), out.get("outcome"))

    ck("控制面没被打死（accept 线程还活着）", server.alive)
    ck("错密钥之后，正常客户端照样能用", _wait(lambda: _can_call(), 5),
       "last_error=%s" % server.last_error)
    ck("服务端记下了这次握手失败（可查）", bool(server.last_error),
       "last_error=%s" % server.last_error)
    server.stop()


def _can_call() -> bool:
    try:
        return bridge.call("status", timeout=2.0) == {"ok-here": True}
    except bridge.BridgeError:
        return False


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def main() -> int:
    # 整轮自测跑在一条测试专用管道上：不碰真身的控制面。
    os.environ[bridge.PIPE_NAME_ENV] = r"\\.\pipe\VoicePill-selftest-%d" % os.getpid()
    _require_test_pipe()
    ck = Checker()
    print("=== 控制面（命名管道）自测 ===")
    print("本次用的管道名：%s" % bridge.pipe_name())
    for fn in (check_pipe_name, check_protocol, check_bad_key):
        print("\n[%s]" % title(fn))
        fn(ck)
    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())