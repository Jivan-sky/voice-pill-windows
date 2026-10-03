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

import os
import sys

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


def main() -> int:
    ck = Checker()
    print("=== NDJSON 控制面通道自测 ===")
    for fn in (check_pipe_name, check_auth_token):
        print("\n[%s]" % title(fn))
        fn(ck)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())