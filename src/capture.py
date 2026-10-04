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


def dest_label(dest: str) -> str:
    """落点路径 → 笔记里那个可读的名字（`...\\00_Inbox` → `00_Inbox`）。

    frontmatter 里不写机器路径：路径是这台机器的事，而笔记是要跟着知识库走
    的。记「落在哪个捕获区」就够了，落到哪个盘由配置说了算。
    """
    return os.path.basename(os.path.normpath(dest)) if dest else ""


def _yaml_quoted(value: str) -> str:
    """塞进 YAML 双引号标量。理由里带「」和全角标点，不加引号会被当成语法。

    换行也必须转义：双引号标量里的裸换行会被 YAML 折成空格，理由就被悄悄
    改写了——不报错、只是内容变了，这种最难查。转义后能原样读回来。
    """
    return '"%s"' % ((value or "").replace("\\", "\\\\").replace('"', '\\"')
                     .replace("\n", "\\n").replace("\r", "\\r")
                     .replace("\t", "\\t"))


def compose(body: str, now: datetime,
            route_meta: Optional[Tuple[str, str]] = None) -> str:
    """拼出一篇捕获笔记的完整内容：frontmatter + 正文 + 来源行。

    正文里的换行统一成 LF：ASR 或剪贴板给的换行不一定是 `\n`，而落盘必须
    保持纯 LF —— `write` 的 `newline="\n"` 只拦得住 Python 自己那层转换，
    拦不住正文里自带的 `\r`。

    `route_meta` 是 `(落点, 理由)`，就是 `route` 的前两项。给了就多写两行
    frontmatter：`route:` 记落在哪个捕获区，`reason:` 记为什么落这——issue #1
    的验收里「路由可解释」靠的就是这一行白纸黑字。**不给则输出与今天逐字节
    相同**，老调用方一行都不用改。

    为什么理由和落点写在同一篇里，而不是另开一本日志：理由只对**这一篇**成
    立，跟着笔记走才不会被后来的改动对不上号。
    """
    stamp = now.strftime("%Y-%m-%d %H:%M")
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [
        '---\n',
        'title: "口述捕获 %s"\n' % stamp,
        'created: %s\n' % now.strftime("%Y-%m-%d"),
        'tags:\n',
        '  - type/inbox\n',
    ]
    if route_meta is not None:
        dest, reason = route_meta
        label = dest_label(dest)
        if label:
            lines.append('route: %s\n' % _yaml_quoted(label))
        lines.append('reason: %s\n' % _yaml_quoted(reason))
    lines.extend(['---\n', '\n', '%s\n' % text, '\n',
                  '> 口述捕获 · %s\n' % stamp])
    return ''.join(lines)


def write(inbox_dir: str, body: str, now: Optional[datetime] = None,
          route_meta: Optional[Tuple[str, str]] = None) -> str:
    """把一次捕获原子地写进 `inbox_dir`，返回落盘路径。

    用 `O_CREAT|O_EXCL` 创建，**绝不覆盖已有文件**：同一秒的第二次捕获自动
    退到 `-2`、`-3`……。写坏的文件会被删掉，不留半个残件。

    `route_meta` 原样转给 `compose`（见那里的说明）；不给就是今天的行为。
    """
    if not inbox_dir:
        raise OSError("没配置 capture_dir（落库目标目录），不知道往哪写")
    when = now or datetime.now()
    os.makedirs(inbox_dir, exist_ok=True)
    content = compose(body, when, route_meta)
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


def route(text: str, routes: Optional[dict] = None, default_dir: str = "",
          keywords: Optional[dict] = None) -> Tuple[str, str, str]:
    """这次落哪、以及为什么落这。纯函数，不落盘、不看配置。

    返回 `(dest, reason, suggestion)`：

    - `dest`：目标目录。只可能来自 `routes` 的**显式映射**，否则就是 `default_dir`。
    - `reason`：一句人话，写进 frontmatter 的 `reason:` 键。issue #1 的验收里
      有一条「路由可解释：能说清为什么落在这篇」——理由要和落点同一次产出，
      不能事后补。理由里出现的落点一律走 `dest_label` 收成目录名：`reason`
      和 `route` 写在同一篇 frontmatter 里，一个写短名、一个漏机器路径，
      等于自己打自己。
    - `suggestion`：关键词给出的**建议**落点，只作提示、**不参与 `dest`**。

    为什么关键词不出落点：`记一下 明天的计划` 会命中「计划」，但它其实是个人
    待办、该进 Inbox；`把刚才的决策过程记一下` 命中「决策」，实际是过程记录。
    关键词命中的是**词**，不是**意图**，拿它定落点只会让「可解释」退化成
    「可自圆其说」。所以关键词只出建议，落点只认显式口令。

    为什么不做内容分类（落 `03_Knowledge/` 等终态目录）：知识库自己的约定是
    「先扔进来，别分类，每周清空一次」（`00_Inbox/_INBOX.md`），分类要有模型
    在场，而这里是常驻引擎，不该背一个 LLM。`docs/superpowers/specs/2026-10-03-
    口述落库.md` §2 也把「自动分类到 `03_Knowledge/` 等最终目录」划在范围外
    —— 那是 `/process-inbox` 与 `save-*` 技能的事。

    `routes` 形如 `{"记一下存项目": r"..."}`。命中多个时**最长者胜**：更具体的
    口令应当压过更笼统的（`记一下存项目` 压过 `记一下`）。判定与 `parse` 同形
    （先剥开头标点、再 `startswith`），只多一条排序，不另立一份会走散的规则。

    未配置 `routes` 时行为与今天完全一致：落 `default_dir`。

    注意：非 Inbox 的 `dest` 还需要对应的 `type/` 标签，而 `compose` 把
    `type/inbox` 写死。所以本期 `routes` 只应指向 Inbox 目录；要多落点，得先
    把 `type/` 标签变成 `compose` 的入参，那一步不在本期范围。
    """
    body = _strip_leading(text)
    if not body:
        return default_dir, "空文本，落默认捕获区", ""

    pairs = sorted(
        ((_strip_leading(p), d) for p, d in (routes or {}).items() if _strip_leading(p)),
        key=lambda pd: len(pd[0]), reverse=True)
    for prefix, dest in pairs:
        if body.startswith(prefix):
            return dest or default_dir, "口令「%s」指定落点" % prefix, ""

    suggestion, word = "", ""
    for kw, dest in (keywords or {}).items():
        if kw and kw in body:
            suggestion, word = dest, kw
            break
    if suggestion:
        return (default_dir,
                "无口令命中；关键词「%s」建议 %s，建议不生效"
                % (word, dest_label(suggestion)),
                suggestion)
    return default_dir, "无口令命中，落默认捕获区", ""