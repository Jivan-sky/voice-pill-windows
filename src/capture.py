# -*- coding: utf-8 -*-
"""口述落库 —— 把以口令开头的那句话，落进知识库的捕获区（Obsidian Inbox）。

为什么只做这两件事
------------------
知识库自己的约定是「先扔进来，别分类，每周清空一次」（`00_Inbox/_INBOX.md`）。
分类要有模型在场，而这里是常驻引擎，不该背一个 LLM。所以本模块只负责：
判定是不是一次捕获、以及原子地写出一个文件。

为什么全是纯函数
----------------
不 import 引擎里任何东西，也不碰 Windows API —— 于是能脱离窗口单测
（见 `tools/capture-selftest.py`），将来外壳换语言也能原样搬走。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional, Sequence, Tuple

# ASR 常在开头带一个标点（「，」「：」之类），判定前先把这些字符剥掉。
_LEADING = "，。、：；！？,.:;!? \t\u3000\"'“”‘’「」『』（）()【】[]"

# 同一秒内的第几次捕获，最多退到这个序号
_MAX_SEQ = 100


def _strip_leading(text: str) -> str:
    """去掉首尾空白，以及开头粘着的一串标点。

    一次 `lstrip` 就够：标点集合里已经含空白，剥完首字符必不在集合内。
    """
    return (text or "").strip().lstrip(_LEADING)


def parse(text: str, prefixes: Sequence[str]) -> Tuple[bool, str]:
    """判定这次转写是不是一次捕获，同时把口令剥掉。

    返回 `(是否命中, 正文)`。正文可以是空串——空捕获也好过一个字都不留。
    口令在句子中间不算命中：那是正常说话，不是要落库。
    """
    body = _strip_leading(text)
    if not body:
        return False, ""
    for prefix in prefixes or ():
        prefix = _strip_leading(prefix)
        if prefix and body.startswith(prefix):
            return True, _strip_leading(body[len(prefix):])
    return False, ""


def decide(text: str, prefixes: Sequence[str], armed: bool = False) -> Tuple[bool, str]:
    """这次转写要不要落库，正文是什么。

    `armed` 是「手指打的标记」（双击 Fn）。标记亮着就落库，且**不再要求
    句子以口令开头** —— 口令要靠 ASR 把「记一下」听对，而英文/技术词常
    被听歪（见 issue #3）；手指不会听错。
    唯一保留的口令处理：标记亮着时顺口说了口令，也把它剥掉 —— 那是触发
    词，不是内容。标记与口令都没有 → 普通说话，不落库。
    """
    hit, body = parse(text, prefixes)
    if hit:
        return True, body
    if not armed:
        return False, ""
    body = _strip_leading(text)
    return (True, body) if body else (False, "")


def compose(body: str, now: datetime) -> str:
    """拼出一篇捕获笔记的完整内容：frontmatter + 正文 + 来源行。

    正文里的换行统一成 LF：ASR 或剪贴板给的换行不一定是 `\n`，而落盘必须
    保持纯 LF —— `write` 的 `newline="\n"` 只拦得住 Python 自己那层转换，
    拦不住正文里自带的 `\r`。
    """
    stamp = now.strftime("%Y-%m-%d %H:%M")
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return (
        '---\n'
        'title: "口述捕获 %s"\n'
        'created: %s\n'
        'tags:\n'
        '  - type/inbox\n'
        '---\n'
        '\n'
        '%s\n'
        '\n'
        '> 口述捕获 · %s\n'
    ) % (stamp, now.strftime("%Y-%m-%d"), text, stamp)


def write(inbox_dir: str, body: str, now: Optional[datetime] = None) -> str:
    """把一次捕获原子地写进 `inbox_dir`，返回落盘路径。

    用 `O_CREAT|O_EXCL` 创建，**绝不覆盖已有文件**：同一秒的第二次捕获自动
    退到 `-2`、`-3`……。写坏的文件会被删掉，不留半个残件。
    """
    if not inbox_dir:
        raise OSError("没配置 capture_dir（落库目标目录），不知道往哪写")
    when = now or datetime.now()
    os.makedirs(inbox_dir, exist_ok=True)
    content = compose(body, when)
    stem = when.strftime("%Y-%m-%d-%H%M%S")
    for seq in range(1, _MAX_SEQ):
        name = "%s.md" % stem if seq == 1 else "%s-%d.md" % (stem, seq)
        path = os.path.join(inbox_dir, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return path
    raise OSError("同一秒里同名文件太多，放弃：%s" % stem)