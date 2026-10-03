# -*- coding: utf-8 -*-
"""假引擎：实现 NDJSON 契约，但不做任何转写。

为什么需要它
------------
M1 通路是「按住 Fn → 录音 → 子进程 → 解码 → 粘贴」。真引擎要么等 ChatGPT
登录，要么被豆包服务端拒（见 docs/移植方案.md 6.4）。而链路里除转写之外
的每一段——钩子、采音、重采样、管道背压、NDJSON 解码、粘贴——都跟用的是
哪个引擎无关。用假引擎就能把那些先钉死，等凭据到位只剩最后一跳要验。

它把「收到多少字节音频」当成转写内容回吐，所以粘出来的文字本身就是证据：
数字对不上，说明管道丢包。

用法
----
    set "VOICEPILL_ENGINE=D:\\Own_tools&skills\\voice-pill-windows\\tools\\mock-asr.py"
    .venv\\Scripts\\python.exe src\\main.py

    （引号别省。路径里的 `&` 在 cmd 下是命令分隔符，`set` 会在那断句，
    赋进去的值只剩 `D:\\Own_tools`；bash 下裸 & 是后台运算符，同样断。）

单独试：
    cat tmp/test-24k.pcm | python tools/mock-asr.py stream --sample-rate 24000
"""
from __future__ import annotations

import json
import sys
import time

# 长按阈值之上、真引擎之下，模拟一段转写耗时，好把「转写中」状态也走到
FINAL_DELAY = 0.4


def _force_utf8() -> None:
    """stdout 钉死 UTF-8。

    本脚本要按 NDJSON 契约吐中文，而 stdout 一旦被管道接走，Python 就改用
    locale 编码（本机 GBK）→ 父进程按 UTF-8 解，直接
    `'utf-8' codec can't decode byte 0xbc`。实测就是这么炸的。

    `live_session.py` 现在会往子进程环境里塞 PYTHONUTF8 / PYTHONIOENCODING，
    但**假引擎不能依赖调用方**——它就是要用来验契约的，自己先得守规矩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


_force_utf8()


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    rate = 24000
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--sample-rate" and i + 1 < len(argv):
            try:
                rate = int(argv[i + 1])
            except ValueError:
                pass

    # 契约：进程一起来就先发 ready（真引擎也这么干）
    emit({"type": "ready"})

    total = 0
    while True:
        chunk = sys.stdin.buffer.read(4096)
        if not chunk:
            break
        total += len(chunk)

    seconds = total / 2.0 / rate

    # partial 是**整句快照**，不是增量 —— 每发一次都得是到目前为止的全文
    text = "假引擎回显：收到 %d 字节音频，约 %.2f 秒（%d Hz 单声道 16 位）" % (
        total, seconds, rate)
    emit({"type": "partial", "text": text})

    time.sleep(FINAL_DELAY)

    # 两个后端认的完成事件不同：Codex 认 result，豆包认 final。
    # 都发，谁挂上去都能收尾。
    emit({"type": "final", "text": text})
    emit({"type": "result", "text": text})
    return 0


if __name__ == "__main__":
    sys.exit(main())
