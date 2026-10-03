# -*- coding: utf-8 -*-
"""口述落库自测 —— 判定、剥口令、拼装、原子落盘。

不需要窗口、麦克风、网络，只碰一个临时目录：
    .venv\\Scripts\\python.exe tools\\capture-selftest.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import capture                     # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             ("  ← %s" % detail) if detail and not ok else ""))
        if not ok:
            self.failed += 1
        return ok


def check_parse(ck) -> None:
    """判定与剥口令"""
    p = ["记一下"]
    ck("裸口令命中", capture.parse("记一下：明天跟 A 确认接口", p) == (True, "明天跟 A 确认接口"))
    ck("无空格紧贴", capture.parse("记一下明天跟 A 确认接口", p) == (True, "明天跟 A 确认接口"))
    ck("前导标点不影响", capture.parse("，记一下 买牛奶", p) == (True, "买牛奶"))
    ck("空正文也算命中", capture.parse("记一下", p) == (True, ""))
    ck("只有标点也算命中", capture.parse("记一下：", p) == (True, ""))
    ck("没口令不命中", capture.parse("帮我把这行改成异步", p) == (False, ""))
    ck("口令在中间不命中", capture.parse("我记一下这个", p) == (False, ""))
    ck("空文本不命中", capture.parse("", p) == (False, ""))
    ck("None 不命中", capture.parse(None, p) == (False, ""))
    ck("空口令表不命中", capture.parse("记一下 x", []) == (False, ""))
    ck("多口令都匹配时取先者", capture.parse("记一下 x", ["记", "记一下"]) == (True, "一下 x"))
    ck("空白口令混在前面也能命中", capture.parse("记一下 x", ["", "  ", "记一下"]) == (True, "x"))


def check_compose(ck) -> None:
    """拼装内容"""
    now = datetime(2026, 10, 3, 15, 42, 33)
    got = capture.compose("买牛奶", now)
    ck("有 frontmatter", got.startswith("---\n") and "\n---\n" in got)
    tags = [ln for ln in got.splitlines() if ln.strip().startswith("- type/")]
    ck("tags 段恰好一条 type/inbox", tags == ["  - type/inbox"], repr(tags))
    ck("created 是日期", "\ncreated: 2026-10-03\n" in got)
    ck("标题带时间", 'title: "口述捕获 2026-10-03 15:42"' in got)
    ck("正文在", "\n买牛奶\n" in got)
    ck("带来源行", "> 口述捕获 · 2026-10-03 15:42" in got)
    ck("正文里的 CRLF 被归一成 LF",
       "\r" not in capture.compose("第一行\r\n第二行", now))


def check_write(ck, tmp) -> None:
    """原子落盘"""
    when = datetime(2026, 10, 3, 15, 42, 33)
    p1 = capture.write(tmp, "第一条", when)
    ck("文件名带秒", os.path.basename(p1) == "2026-10-03-154233.md", os.path.basename(p1))
    ck("文件真的在", os.path.isfile(p1))
    with open(p1, "rb") as fh:
        data = fh.read()
    ck("落盘与 compose() 逐字节相等",
       data == capture.compose("第一条", when).encode("utf-8"))
    ck("落盘里没有 CR", b"\r" not in data)
    p2 = capture.write(tmp, "第二条", when)
    ck("同秒不覆盖，退到 -2", os.path.basename(p2) == "2026-10-03-154233-2.md",
       os.path.basename(p2))
    with open(p1, "rb") as fh:
        ck("第一条一字未变", fh.read() == data)
    p3 = capture.write(tmp, "甲\r\n乙", when)
    ck("第三次退到 -3", os.path.basename(p3) == "2026-10-03-154233-3.md",
       os.path.basename(p3))
    with open(p3, "rb") as fh:
        ck("正文带 CRLF，落盘也不留 CR", b"\r" not in fh.read())
    deep = os.path.join(tmp, "深", "一层")
    ck("目录不存在会自动建", os.path.isfile(capture.write(deep, "x", when)))
    try:
        capture.write("", "x", when)
        ck("空目录要报错", False)
    except OSError:
        ck("空目录要报错", True)


def check_write_cleanup(ck, tmp) -> None:
    """写坏了不留残件"""
    where = os.path.join(tmp, "cleanup")
    real = capture.compose
    capture.compose = lambda body, now: "\ud800"      # 故意给个编不成 UTF-8 的
    try:
        capture.write(where, "x", datetime(2026, 10, 3, 15, 42, 33))
        ck("编码失败要抛", False, "没有抛异常")
    except UnicodeEncodeError:
        ck("编码失败要抛", True)
    except Exception as exc:                          # noqa: BLE001
        ck("编码失败要抛", False, repr(exc))
    finally:
        capture.compose = real
    ck("失败后目标目录是空的", os.path.isdir(where) and os.listdir(where) == [],
       repr(os.listdir(where) if os.path.isdir(where) else None))


def main() -> int:
    ck = Checker()
    tmp = tempfile.mkdtemp(prefix="vp-capture-")
    try:
        print("=== 口述落库自测 ===")
        for fn, args in ((check_parse, ()), (check_compose, ()),
                         (check_write, (tmp,)), (check_write_cleanup, (tmp,))):
            print("\n[%s]" % fn.__doc__.strip().splitlines()[0])
            fn(ck, *args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())