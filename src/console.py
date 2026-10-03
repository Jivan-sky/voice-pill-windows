# -*- coding: utf-8 -*-
"""控制台输出的编码兜底。

问题
----
Windows 中文环境下，`sys.stdout` 一旦被重定向到管道或文件，Python 就改用
locale 编码（本机 GBK）。此时打印 ✅ / ❌ / 🎤 这类符号会直接抛
`UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'`，**整个
进程崩掉**——不是显示成问号，是崩。

本工程的横幅、自检、自测里到处是这类符号，不能靠调用方记得设
`PYTHONIOENCODING`。踩过三次（`main.py --check`、`shell-selftest.py`、
手动跑探针），所以抽到这里统一兜。

策略
----
* 重定向（非 tty）→ 强制 UTF-8 **且改为行缓冲**。日志文件按 UTF-8 读就对了。
* 真控制台 → 保留原编码，只把编不出的字符降级成 `?`。
  这样中文在 GBK 控制台上仍是中文，不会为了迁就几个符号把中文变乱码。

行缓冲是必须的
--------------
重定向时 Python 默认**块缓冲**（8 KB 一刷），于是 `python main.py | tee log`
的启动横幅一行都看不见，看着像卡死了——实测跑管道自测时就撞上：命令打出去
一分半，输出文件是空的，因为进程还在缓冲里憋着。`flush=True` 要求每个
print 都记得写，靠不住；`reconfigure(line_buffering=True)` 一次解决。

用法：在打印任何东西之前调用一次。
    import console
    console.make_output_safe()
"""
from __future__ import annotations

import sys


def make_output_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True)
        except (AttributeError, OSError, ValueError):
            # 流被替换过、或已关闭：放弃兜底，不影响主流程
            pass
