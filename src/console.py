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

驻留形态还要多一步
----------------
开机自启用的是 `pythonw.exe`（GUI 子系统，不弹黑窗口），由计划任务拉起时
`sys.stdout` / `sys.stderr` 都是 `None`——`print()` 变成静默空操作。这时进程
崩了、钩子装不上了，用户什么都看不到，只剩"它怎么不work了"。

所以驻留进程一律调 `attach_app_log()`：**总是**往
`%LOCALAPPDATA%\VoicePill\logs\app.log` 记一份账，有控制台就再镜像一份到屏幕。

为什么是"总是"而不是"仅在无控制台时"
--------------------------------
最初写成"只有 stdout 为 None 才记账"，被自测抓出问题：`pythonw` 被**有控制台
的父进程**拉起时会继承那个控制台，`sys.stdout` 就不是 None 了——于是同一条
命令，从任务计划启动有账、从控制台启动没账。**有没有黑匣子取决于谁拉起来的**，
这种不确定性比没有黑匣子更糟。所以改成无条件记账，日志文件因此总是存在。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

# 应用日志（驻留形态的"黑匣子"）保留策略
APP_LOG_MAX_BYTES = 2 * 1024 * 1024
APP_LOG_KEEP = 3                 # 连同当前文件，最多留 4 个

# 当前挂着的日志文件（进程内只会有一个）。用列表而不是全局变量赋值，
# 是为了能在函数里改而不需要 global 声明。
_LOG_HANDLES: list = []


class _Tee:
    """同时写日志文件和原来的流（有控制台时用）。

    只实现 print/flush 真正用到的那几个方法。`reconfigure` 做成空操作：
    make_output_safe() 可能再被调一次，落到这里不该炸。
    """

    def __init__(self, log, stream) -> None:
        self._log = log
        self._stream = stream

    def write(self, text: str) -> int:
        for target in (self._log, self._stream):
            try:
                target.write(text)
            except (OSError, ValueError):
                pass            # 屏幕那头没了不该拖死日志那头
        return len(text)

    def flush(self) -> None:
        for target in (self._log, self._stream):
            try:
                target.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **_kwargs) -> None:
        pass


def rotate_if_needed(path: str, max_bytes: int = APP_LOG_MAX_BYTES,
                     keep: int = APP_LOG_KEEP) -> None:
    """按大小滚动：app.log → app.log.1 → app.log.2 …（最老的直接丢）。

    刻意不用 logging 模块的 RotatingFileHandler：这里的输出是被重定向的
    `print`，不是 logging 记录，两者混起来会打架（同一个文件两个写入者、
    两套轮转规则）。就 20 行，自己滚更直白。
    """
    try:
        if os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return

    try:
        os.remove("%s.%d" % (path, keep))
    except OSError:
        pass
    for i in range(keep - 1, 0, -1):
        src = "%s.%d" % (path, i)
        if os.path.isfile(src):
            try:
                os.replace(src, "%s.%d" % (path, i + 1))
            except OSError:
                pass
    try:
        os.replace(path, path + ".1")
    except OSError:
        pass


def attach_app_log(path: str, label: str = "") -> bool:
    """开始记账：日志文件总是写；有控制台时同时镜像到屏幕。

    返回 True = 挂上了（文件打不开时为 False，此时**不改** stdout/stderr，
    免得把输出推进黑洞）。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    rotate_if_needed(path)
    try:
        fh = open(path, "a", encoding="utf-8", errors="replace",
                  buffering=1)
    except OSError:
        return False

    _LOG_HANDLES.append(fh)
    sys.stdout = fh if sys.stdout is None else _Tee(fh, sys.stdout)
    sys.stderr = fh if sys.stderr is None else _Tee(fh, sys.stderr)

    if label:
        # 每次启动打一条时间戳横幅。崩了就没有对应的"退出"行，
        # 这是判断"是崩了还是被正常停掉"的唯一依据。
        print("\n=== %s pid=%d %s ==="
              % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 os.getpid(), label))
    return True


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
