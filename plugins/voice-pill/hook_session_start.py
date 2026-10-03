"""SessionStart 钩子：Codex 一开，把驻留实例拉起来，并把它的命绑在 Codex 上。

它做两件事：

1. 沿父链找出**最外层的** Codex 进程（`codex.exe` 上面那个桌面壳
   `ChatGPT.exe`），把 (pid, 映像路径) 写进锚点纸条。看门狗照着纸条盯人，
   人没了就请驻留实例收摊。原理与取舍见工程 `src/anchor.py` 顶部。
2. 确保看门狗在跑。看门狗有互斥体，已经在守就什么都不做——所以这条钩子
   每次开新会话跑一遍是幂等的，「开机自启」在登录时拉起的那个也不会被
   重复拉起。

输出：**任何情况下**都要给 Codex 一行 `{"continue": true}`。钩子卡住会拖慢
会话启动，所以这里所有异常都吞掉，只写日志。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(r"D:\Own_tools&skills\voice-pill-windows")
PROJECT_SRC = PROJECT_ROOT / "src"
# 拉起看门狗必须用**工程**的 venv（引擎的依赖全在里面），不是插件那个
# 只装了 mcp 的 venv。
PYTHONW = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
SUPERVISE = PROJECT_ROOT / "src" / "supervise.py"

LOG_PATH = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".") \
    / "VoicePill" / "plugin.log"

# 冷启动等它就绪的上限。只在我们**亲手拉起**看门狗时才等——已经在跑的话
# 立刻返回，不拿这几秒去拖慢每一次开新会话。
#
# 为什么是 6 秒：实测这份机器上冷启动（进程起来 → 控制面能应答）落在 3 秒
# 出头，3 秒会差一点点，用户「开了 Codex 立刻按 Fn」就落空。
# 为什么还要盯看门狗：真出故障（模型目录被移走之类）时看门狗会放弃退出，
# 这时候再干等 6 秒就是白等——每开一次会话都要等。所以它一走就立刻收手。
READY_WAIT_SECONDS = 6.0
READY_POLL_SECONDS = 0.3

sys.path.insert(0, str(PROJECT_SRC))

import console                      # noqa: E402

console.make_output_safe()


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


def _read_stdin() -> dict:
    """钩子的输入可能没有（手工调用/换版本）。读不到就当空对象，不报错。"""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_anchor() -> None:
    """把「我在谁的子孙里」写下来。找不到就当没锚点，不拦路。"""
    import anchor
    found = anchor.find_anchor()
    if not found:
        _log("anchor_missing")
        return
    pid, image = found
    anchor.write_note(pid, image)
    _log("anchor", pid=pid, image=os.path.basename(image or ""))


def _ensure_supervisor() -> bool:
    """确保看门狗在跑。返回 True 表示这次是我们亲手拉起来的。"""
    import single_instance
    probe = single_instance.InstanceLock(single_instance.SUPERVISOR_MUTEX_NAME)
    try:
        if not probe.acquire():
            return False                 # 已经有一只守着了
    except OSError:
        return False
    probe.release()

    if not SUPERVISE.is_file():
        _log("supervise_missing", path=str(SUPERVISE))
        return False
    exe = str(PYTHONW) if PYTHONW.is_file() else sys.executable
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([exe, str(SUPERVISE)], cwd=str(PROJECT_ROOT),
                     creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    _log("supervisor_spawned")
    return True


def _supervisor_alive() -> bool:
    """看门狗还在不在守。它放弃了就别再干等。"""
    import single_instance
    probe = single_instance.InstanceLock(single_instance.SUPERVISOR_MUTEX_NAME)
    try:
        if probe.acquire():
            probe.release()
            return False
        return True
    except OSError:
        return True          # 问不出来就别自作主张，照常等完


def _wait_ready() -> bool:
    """等控制面应答。用户在会话刚开起来就按 Fn，是最容易失败的一刻。"""
    import bridge
    deadline = time.monotonic() + READY_WAIT_SECONDS
    seen_alive = False
    while time.monotonic() < deadline:
        try:
            bridge.call("status", timeout=1.0)
            return True
        except Exception:                              # noqa: BLE001
            # 「看门狗不在了就收手」必须等**先见过它活着**才算数：刚 spawn 出来
            # 的那一瞬间它还没拿到互斥体，直接判"不在了"会立刻放弃（实测踩过，
            # 日志里就是 ready_timeout）。
            if _supervisor_alive():
                seen_alive = True
            elif seen_alive:
                return False
            time.sleep(READY_POLL_SECONDS)
    return False


def main() -> int:
    payload = _read_stdin()
    _log("hook_session_start", session=payload.get("session_id"),
         source=payload.get("source"))
    try:
        _write_anchor()
    except Exception as exc:                           # noqa: BLE001
        _log("anchor_error", error=repr(exc))
    try:
        if _ensure_supervisor():
            _log("ready" if _wait_ready() else "ready_timeout")
        else:
            _log("supervisor_already_running")
    except Exception as exc:                           # noqa: BLE001
        _log("start_error", error=repr(exc))
    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
