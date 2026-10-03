# -*- coding: utf-8 -*-
"""Stop 钩子：助手说完一轮，把正文交给常驻实例，让它**念出来**。

为什么是 Stop 而不是别的
------------------------
Codex 的钩子入参里带 `last_assistant_message`（本机 codex.exe 里核过
`hook_event_name` / `last_assistant_message` / `transcript_path`），所以
"助手刚说的那段正文"不用去翻 rollout 文件，钩子手上就有。`Stop` 正好是
一轮结束那一刻——该说话的时候。

两条铁律（都是被别处踩过之后定的）
----------------------------------
1. **这个钩子绝不能拖慢或打断用户的回合。** 它挂在"轮次结束"这条路上，
   一慢就是每次回复都慢。所以：只做一次"排队"调用（常驻实例立刻返回，
   真正的合成与播放在它自己的线程里），超时压到 5 秒，**任何异常都吞掉**。
2. **任何情况下都要往 stdout 打一行 JSON。** 不打，Codex 会等，于是
   "钩子卡住"直接变成"对话卡住"。

开关在哪
--------
"每轮自动念"由常驻实例按 `settings.json` 的 `speak_replies` 决定，本钩子
只负责把 `auto=True` 递过去。**不在这一侧判**——两份规则必然走散。
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

import console                      # noqa: E402

console.make_output_safe()

# 递过去的上限。常驻实例还会再按 speak_max_chars 截一次，这里先砍一道，
# 免得往管道里塞几十 KB（一次超长回复不值得让钩子停留）。
MAX_CHARS = 4000


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


def _say(text: str) -> str:
    """把文本排进发声队列，返回常驻实例的答复。失败一律返回空。"""
    try:
        import bridge
        return str(bridge.call("speak", timeout=5.0, text=text, auto=True))
    except Exception as exc:                              # noqa: BLE001
        _log("hook_stop_skip", error=type(exc).__name__)
        return ""


def main() -> int:
    payload = _read_stdin()
    text = str(payload.get("last_assistant_message") or "").strip()
    _log("hook_stop", session=payload.get("session_id"), text_len=len(text))

    if not text:
        # 有些轮次没有正文（工具轮、被打断）。没什么可念的，放行。
        print(json.dumps({"continue": True}))
        return 0

    reply = _say(text[:MAX_CHARS])
    if reply:
        _log("hook_stop_spoken", reply=reply[:120])
    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())