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


def check_decide(ck) -> None:
    """标记与口令两条路的合流（双击 Fn 那条不经过 ASR）"""
    p = ["记一下"]
    ck("有标记：整句当正文，不要求口令",
       capture.decide("明天跟 A 确认接口", p, True) == (True, "明天跟 A 确认接口"))
    ck("有标记：前导标点照剥",
       capture.decide("，明天开会", p, True) == (True, "明天开会"))
    ck("有标记：顺口说了口令也剥掉（触发词不是内容）",
       capture.decide("记一下 买牛奶", p, True) == (True, "买牛奶"))
    ck("有标记：口令表为空也照样落",
       capture.decide("英文 hook 也照样", [], True) == (True, "英文 hook 也照样"))
    ck("有标记：空文本不命中", capture.decide("", p, True) == (False, ""))
    ck("有标记：只有标点不命中", capture.decide("：，", p, True) == (False, ""))
    ck("没标记：退回口令这条路",
       capture.decide("记一下 买牛奶", p, False) == (True, "买牛奶"))
    ck("没标记又没口令：不命中",
       capture.decide("明天开会", p, False) == (False, ""))
    ck("不传标记：默认不落库", capture.decide("明天开会", p) == (False, ""))


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


class _NoRemove:
    """把 `os` 换成「某个目录下的 remove 一律失败」的替身，其余全转发。

    模拟同步盘 / 杀软占着文件：写失败之后的清理也失败。用替身而不是直接改
    `os.remove`，免得把整个进程的 `os` 也一起改了。
    """

    def __init__(self, real, prefix: str) -> None:
        self._real = real
        self._prefix = os.path.abspath(prefix) + os.sep

    def __getattr__(self, name):
        return getattr(self._real, name)

    def remove(self, path, *a, **k):
        if os.path.abspath(str(path)).startswith(self._prefix):
            raise PermissionError(13, "锁着删不掉（自测造的）")
        return self._real.remove(path, *a, **k)


def check_write_cleanup(ck, tmp) -> None:
    """写坏了不留残件"""
    where = os.path.join(tmp, "cleanup")
    real = capture.compose
    # 替身要跟上 compose 的签名（第三个参数是落点/理由，见 capture.route）。
    capture.compose = lambda body, now, route_meta=None: "\ud800"   # 故意给个编不成 UTF-8 的
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

    # 清理也失败：半篇真的留在了盘上。write 必须**说出来**（PartialWrite，带
    # 残件路径），不能吞掉 —— 调用方正是靠「有没有残件」决定认领放不放
    # （见 capture.PartialWrite）。吞掉就会放掉认领，重投写出第二篇。
    residue = os.path.join(tmp, "cleanup-residue")
    real_os = os
    capture.compose = lambda body, now, route_meta=None: "\ud800"
    capture.os = _NoRemove(real_os, residue)
    try:
        capture.write(residue, "x", datetime(2026, 10, 3, 15, 42, 33))
        ck("残件删不掉时要抛 PartialWrite", False, "没有抛异常")
    except capture.PartialWrite as exc:
        ck("残件删不掉时要抛 PartialWrite", True)
        ck("PartialWrite 带上残件路径", os.path.abspath(exc.path).startswith(
            os.path.abspath(residue) + os.sep), repr(getattr(exc, "path", None)))
        ck("残件真的还在盘上（这就是放行第二篇的那条路）",
           os.path.isfile(exc.path), repr(os.listdir(residue)))
        ck("PartialWrite 仍是 OSError（老调用方照旧接得住）",
           isinstance(exc, OSError), repr(type(exc).__mro__))
    except Exception as exc:                          # noqa: BLE001
        ck("残件删不掉时要抛 PartialWrite", False, repr(exc))
    finally:
        capture.compose = real
        capture.os = real_os


def check_index(ck, tmp) -> None:
    """text_id 幂等索引：先认领后落盘、重复投递不写第二篇"""
    idx = os.path.join(tmp, "index")
    ck("第一次认领拿到权利（返回 None）", capture.claim(idx, "t-1") is None)
    # 认领了但没落完，必须报出来：既不当重复（会吞掉该落的字），也不重写
    # （会写出第二篇）。这两条都错得起，所以只能吵。
    try:
        capture.claim(idx, "t-1")
        ck("认领了没落完 → 抛 IncompleteCapture", False, "没有抛异常")
    except capture.IncompleteCapture:
        ck("认领了没落完 → 抛 IncompleteCapture", True)
    except Exception as exc:                          # noqa: BLE001
        ck("认领了没落完 → 抛 IncompleteCapture", False, repr(exc))

    # done 之后才是幂等命中，且要指回第一次那一篇
    capture.commit(idx, "t-1", {"path": r"D:\x\2026-10-05-101010.md",
                                "dest": "00_Inbox", "reason": "无口令命中，落默认捕获区"})
    rec = capture.claim(idx, "t-1")
    ck("落完之后才认成重复", (rec or {}).get("state") == "done", repr(rec))
    ck("重复记录指回第一次那一篇",
       (rec or {}).get("path") == r"D:\x\2026-10-05-101010.md", repr(rec))

    # 兼容性：commit 覆盖掉 pending，读回来的是 done 且字段齐
    ck("commit 后 dest/reason 也存下来了",
       (rec or {}).get("dest") == "00_Inbox" and "默认捕获区" in (rec or {}).get("reason", ""),
       repr(rec))

    # release：只放 pending 的
    capture.claim(idx, "t-2")
    capture.release(idx, "t-2")
    ck("落盘失败后能重新认领", capture.claim(idx, "t-2") is None)
    capture.commit(idx, "t-3", {"path": "p"})
    capture.release(idx, "t-3")
    ck("已 done 的不被 release 抹掉", (capture.claim(idx, "t-3") or {}).get("path") == "p")

    # 认领文件坏了：不许猜，当没落完
    bad = os.path.join(idx, "%s.json" % capture._text_id_key("t-4"))
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 JSON")
    try:
        capture.claim(idx, "t-4")
        ck("坏掉的认领记录要抛", False, "没有抛异常")
    except capture.IncompleteCapture:
        ck("坏掉的认领记录要抛", True)
    except Exception as exc:                          # noqa: BLE001
        ck("坏掉的认领记录要抛", False, repr(exc))

    # text_id 是外部给的字符串，不能拿它直接拼路径
    weird = capture._index_file(idx, "../../../etc/passwd")
    ck("text_id 带路径分隔符也跑不出索引目录",
       os.path.dirname(os.path.abspath(weird)) == os.path.abspath(idx), weird)
    ck("同一个 text_id 的键是稳定的",
       capture._text_id_key("a-b") == capture._text_id_key("a-b"))
    ck("不同 text_id 不撞键", capture._text_id_key("a") != capture._text_id_key("b"))


def check_write_with_route(ck, tmp) -> None:
    """落点与理由真的落到了 frontmatter（issue #1 的「路由可解释」）"""
    where = os.path.join(tmp, "routed")
    dest, reason, _s = capture.route("记一下存项目：接口定了", {"记一下存项目": where},
                                     default_dir=os.path.join(tmp, "inbox"))
    path = capture.write(dest, "接口定了", datetime(2026, 10, 5, 10, 10, 10),
                         route_meta=(dest, reason))
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    ck("落到了口令指定的目录", os.path.dirname(path) == where, path)
    ck("frontmatter 里有 route 行", 'route: "routed"' in text, text)
    ck("frontmatter 里有 reason 行", 'reason: "' in text, text)
    ck("理由里不留机器路径", tmp not in text, text)


def main() -> int:
    ck = Checker()
    tmp = tempfile.mkdtemp(prefix="vp-capture-")
    try:
        print("=== 口述落库自测 ===")
        for fn, args in ((check_parse, ()), (check_decide, ()), (check_compose, ()),
                         (check_write, (tmp,)), (check_write_cleanup, (tmp,)),
                         (check_index, (tmp,)), (check_write_with_route, (tmp,))):
            print("\n[%s]" % fn.__doc__.strip().splitlines()[0])
            fn(ck, *args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s（%d 项失败）"
          % ("✅ 全部通过" if not ck.failed else "❌ 有失败", ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())