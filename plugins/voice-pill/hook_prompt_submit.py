"""UserPromptSubmit 钩子：把用户刚用语音说的话，作为附加上下文注入本轮对话。

为什么用 command 型钩子而不是 mcp_tool 型：后者在本机这个 Codex 版本上
实测只报 `hook: UserPromptSubmit Failed`（且 `feature plugin_hooks` 已标记
removed），而 command 型不依赖那条链路。

输入：Codex 从 stdin 给一份 JSON（含 prompt / session_id / turn_id 等）。
输出：stdout 打一份 hook 输出 JSON（`hookSpecificOutput.additionalContext`）。
没有待取文字时打 `{"continue": true}`——**任何情况下都要有输出**，钩子卡住
会拖慢用户的每一次回车。

stdout 必须是 UTF-8：Codex 按 UTF-8 读这一行，而这个进程的 stdout 是管道，
Python 默认会退回 locale 编码（本机 GBK）。见下面 console.make_output_safe()。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_SRC = Path(r"D:\Own_tools&skills\voice-pill-windows\src")
LOG_PATH = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".") \
    / "VoicePill" / "plugin.log"

sys.path.insert(0, str(PROJECT_SRC))

# stdout 一旦是管道（Codex 就是这么读的），Python 会退回 locale 编码（本机 GBK），
# 而 Codex 按 UTF-8 读。实测后果不是报错，是**中文提示语整段变成乱码**塞进模型
# 上下文——静默生效，比崩掉更难发现。console.make_output_safe 是本工程统一的兜法。
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


def main() -> int:
    payload = _read_stdin()
    _log("hook_prompt_submit", session=payload.get("session_id"),
         turn=payload.get("turn_id"), prompt_len=len(payload.get("prompt") or ""))

    try:
        import bridge
        taken = bridge.call("take", timeout=5.0)
    except Exception as exc:                              # noqa: BLE001
        # 引擎没开、管道断了——绝不能因此拦住用户的这一轮对话。
        _log("hook_prompt_submit_skip", error=str(exc))
        print(json.dumps({"continue": True}))
        return 0

    texts = [t for t in (taken.get("texts") or []) if t.strip()]
    if not texts:
        print(json.dumps({"continue": True}))
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "用户刚刚用语音输入（Voice Pill）说了以下内容，"
                "请把它当作本轮意图的一部分：\n" + "\n".join(texts)
            ),
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
