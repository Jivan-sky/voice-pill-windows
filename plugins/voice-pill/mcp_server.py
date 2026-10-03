"""Voice Pill 的 Codex 插件服务端。

它自己不做语音：采音、转写、粘贴全在工程里那个已常驻的进程（
`D:\\Own_tools&skills\\voice-pill-windows`）里，这里只是它的控制面客户端，
把它变成 Codex 能调的工具。

为什么这么切：语音引擎那侧要装模型、Argon 二进制、低级键盘钩子，还要在
Codex 没开着的时候也能用。硬塞进插件进程只会得到两份实现和两份状态。

连接方式见工程 `src/bridge.py` 顶部：命名管道 + authkey，一行 JSON 一问一答。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

# 工程位置。插件目录会被整包拷进 Codex 的 cache，所以这里只能写绝对路径
# （实测 `.mcp.json` 里的相对 command 会直接报 os error 3）。
PROJECT_ROOT = Path(r"D:\Own_tools&skills\voice-pill-windows")
PROJECT_SRC = PROJECT_ROOT / "src"

# 引擎没在跑时由插件把它拉起来。和开机自启用的是同一条命令（看门狗 +
# 真身），所以这里拉起来的和开机起来的完全等价。
PYTHONW = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
SUPERVISE = PROJECT_ROOT / "src" / "supervise.py"

# 录音结束后等转写的最长时间。工程里 local 后端的收尾超时是 30 秒
# （providers.LOCAL.finish_timeout），这里留一倍余量。
TRANSCRIBE_TIMEOUT = 60.0
POLL_INTERVAL = 0.3

sys.path.insert(0, str(PROJECT_SRC))

# 调用留痕。Codex 的事件（比如 UserPromptSubmit 钩子）会不会真的打进来，
# 从 Codex 那一侧看不见；有一行本地日志就能事后确认，不用靠猜。
LOG_PATH = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".") \
    / "VoicePill" / "plugin.log"


def _log(event: str, **fields: object) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        parts = " ".join("%s=%s" % (k, v) for k, v in fields.items())
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("%s pid=%d %s %s\n"
                     % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        os.getpid(), event, parts))
    except OSError:
        pass


def _bridge():
    """延迟导入：把「工程不在了」变成一个能说人话的工具错误，而不是
    服务端启动就直接死掉——后者在 Codex 里表现为整个插件不可用，看不出原因。
    """
    try:
        import bridge                                    # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "找不到 Voice Pill 工程（%s）：%s。请确认工程还在原位置。"
            % (PROJECT_SRC, exc)
        ) from exc
    return bridge


def _call(cmd: str, timeout: float = 15.0, **args):
    _log("bridge", cmd=cmd)
    try:
        return _bridge().call(cmd, timeout=timeout, **args)
    except Exception as exc:                             # noqa: BLE001
        # bridge.BridgeError 之外也可能是 RuntimeError（工程缺失），一并转成
        # 对模型友好的文本，别让 MCP 层只回一个栈。
        raise RuntimeError(str(exc)) from exc


def _ensure_engine() -> dict:
    """引擎没在跑就拉起来，返回拉起后的状态。"""
    bridge = _bridge()
    state = bridge.probe(timeout=4.0)
    if state is not None:
        return state
    if not PYTHONW.is_file() or not SUPERVISE.is_file():
        raise RuntimeError(
            "Voice Pill 没在运行，而且找不到它的启动器：%s / %s"
            % (PYTHONW, SUPERVISE))

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(PYTHONW), str(SUPERVISE)],
                     cwd=str(PROJECT_ROOT), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)

    # 起来要一点时间：看门狗要先起，再由它拉起真身。
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        state = bridge.probe(timeout=2.0)
        if state is not None:
            return state
        time.sleep(0.5)
    raise RuntimeError("拉起 Voice Pill 之后 20 秒仍然连不上它的控制面。")


def _wait_for_result(timeout: float = TRANSCRIBE_TIMEOUT) -> dict:
    """等这次录音走完（不再处于 recording/transcribing），然后把文字取走。"""
    bridge = _bridge()
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        state = bridge.call("status", timeout=8.0)
        if state.get("phase") == "idle":
            break
        time.sleep(POLL_INTERVAL)
    taken = bridge.call("take", timeout=8.0)
    texts = taken.get("texts") or []
    return {
        "text": texts[-1] if texts else "",
        "texts": texts,
        "timed_out": bool(state and state.get("phase") != "idle"),
        "phase": (state or {}).get("phase"),
    }


mcp = MCPServer("voice_pill")


@mcp.tool()
def voice_pill_status() -> dict:
    """查看 Voice Pill 常驻引擎的状态：是否在运行、当前阶段、后端、热键、待取文字条数。"""
    bridge = _bridge()
    state = bridge.probe(timeout=4.0)
    if state is None:
        return {"running": False,
                "note": "常驻引擎没在跑。调用 voice_pill_listen 会自动把它拉起来。"}
    return {"running": True, **state}


@mcp.tool()
def voice_pill_listen(seconds: float = 10.0) -> dict:
    """让常驻引擎录一段音并返回转写文字。

    适用于想直接用语音口述一段内容给我。录音会在 `seconds` 秒后自动结束；
    期间用户自己按 Fn 松手也会提前结束。返回 text 为空表示这段没人说话，
    或麦克风没出声。
    """
    _ensure_engine()
    _call("start", timeout=15.0)
    deadline = time.monotonic() + max(0.5, float(seconds))
    while time.monotonic() < deadline:
        time.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
        state = _call("status", timeout=8.0)
        if state.get("phase") == "idle":
            break                      # 用户已经按 Fn 松手结束了这次
    _call("stop", timeout=15.0)
    return _wait_for_result()


@mcp.tool()
def voice_pill_stop() -> dict:
    """结束正在进行的录音并返回转写文字（若当时没在录音，则返回队列里待取的文字）。"""
    state = _ensure_engine()
    if state.get("phase") != "idle":
        _call("stop", timeout=15.0)
    return _wait_for_result()


@mcp.tool()
def voice_pill_cancel() -> dict:
    """丢弃正在进行的录音，不转写、不粘贴。"""
    _call("cancel", timeout=15.0)
    return _call("status", timeout=8.0)


@mcp.tool()
def voice_pill_take() -> dict:
    """取走并清空「待取文字」（用户此前按 Fn 说过的、已经粘贴过的那些）。

    取走语义：同一个第二次调用不会重复拿到。用于用户已经用 Fn 说完了、
    再由我来读取内容的场景。只含保鲜期（默认 30 分钟，见
    settings.pending_ttl_minutes）内的字：更早的已经作废、取不回来。
    """
    return _call("take", timeout=8.0)


@mcp.tool()
def voice_pill_prompt_hook(hook_event_name: str = "", session_id: str = "",
                           turn_id: str = "", cwd: str = "",
                           prompt: str = "") -> dict:
    """给 UserPromptSubmit 钩子用的：把待取文字作为本轮附加上下文注入。

    由 Codex 的事件驱动调用，不是给模型主动调的。没有待取文字时原样放行。
    """
    _log("prompt_hook", session=session_id, turn=turn_id,
         prompt_len=len(prompt))
    try:
        taken = _call("take", timeout=6.0)
    except Exception:                                    # noqa: BLE001
        # 钩子绝不能因为引擎没开就拦住用户的这一轮对话。
        return {"continue": True}

    texts = [t for t in (taken.get("texts") or []) if t.strip()]
    if not texts:
        return {"continue": True}
    body = "\n".join(texts)
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "用户刚刚用语音输入（Voice Pill）说了以下内容，"
                "请把它当作本轮意图的一部分：\n" + body
            ),
        }
    }


if __name__ == "__main__":
    _log("server_start", argv=sys.argv, cwd=os.getcwd())
    try:
        mcp.run()
    finally:
        _log("server_stop")
