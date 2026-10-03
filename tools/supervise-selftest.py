# -*- coding: utf-8 -*-
"""「和 Codex 共同启停」自测：认人、句柄、纸条、换人、收摊、兜底。

为什么需要它
------------
这条能力最难查的坏法全是**静默**的：锚点认错人 → 关掉别的程序把语音一起
带走；PID 复用没防住 → 半天之后突然自己退了；纸条读不出来 → 绑不上却看不出
来（退回常驻，用户以为绑上了）。所以这里不测「正常路径通不通」，测的是
**认人的边界**和**该收摊的时候真收得掉**。

全程不碰真身：纸条指向一个临时文件，收摊信号打在假的 single_instance 上。

用法：
    .venv\\Scripts\\python.exe tools\\supervise-selftest.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import anchor                      # noqa: E402
import supervise                   # noqa: E402


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


# 一个只等着被"停"的假真身：哨兵文件一出现就正常退出（code 0）。
_SLEEPER = (
    "import os, sys, time\n"
    "p = os.environ.get('VP_SENTINEL') or ''\n"
    "end = time.monotonic() + 60\n"
    "while time.monotonic() < end:\n"
    "    if p and os.path.exists(p):\n"
    "        sys.exit(0)\n"
    "    time.sleep(0.05)\n"
    "sys.exit(3)\n"
)

# 一个不理会任何信号的假真身：只有 terminate/kill 收得掉。
_STUBBORN = ("import time\n"
             "time.sleep(60)\n")


class FakeSingle:
    """假的 single_instance：收摊信号打在这上面，不碰整机的命名事件。"""

    def __init__(self, sentinel: str = "") -> None:
        self.sentinel = sentinel
        self.quit_calls = 0

    def request_quit(self) -> bool:
        self.quit_calls += 1
        if self.sentinel:
            io.open(self.sentinel, "w").close()
        return True


def _spawn(code: str):
    return subprocess.Popen([sys.executable, "-c", code])


def _kill(proc) -> None:
    """连子孙一起杀。

    工程里的 `.venv\\Scripts\\python(w).exe` 是个跳板，会另外起一个 python 再
    自己等在那里；只 kill 跳板会留下一堆还在 sleep 的真身（实测：它们还会
    攥着 stdout 管道不放，把整轮输出卡住）。
    """
    try:
        supervise.kill_tree(proc.pid)
        proc.wait(timeout=10)
    except Exception:                                  # noqa: BLE001
        pass


def check_identify(ck: Checker) -> None:
    """认得对：只认 Codex 家族，且取最外层那个。"""
    ck("codex.exe 算 Codex", anchor.is_codex_image(r"C:\x\codex.exe"))
    ck("ChatGPT.exe 算 Codex", anchor.is_codex_image(
        r"C:\Program Files\WindowsApps\OpenAI.Codex_1\app\ChatGPT.exe"))
    ck("大小写不影响", anchor.is_codex_image(r"C:\X\CODEX.EXE"))
    ck("别的程序不算", not anchor.is_codex_image(r"C:\x\chrome.exe"))
    ck("名字里带 codex 的别的程序不算",
       not anchor.is_codex_image(r"C:\x\codex-computer-use-swift.exe"))
    ck("空路径不算", not anchor.is_codex_image(""))

    chain = anchor.ancestors()
    mine = [(p, i) for p, i in chain if anchor.is_codex_image(i)]
    found = anchor.find_anchor()
    if mine:
        ck("找到的是最外层那个（父链里最后一个 Codex）",
           found == mine[-1], "%r vs %r" % (found, mine[-1]))
    else:
        ck("当前不在 Codex 子孙里时，返回 None（不瞎认）", found is None,
           repr(found))


def check_handle(ck: Checker) -> None:
    """认人不认号：句柄钉住进程对象，映像对不上就拒收。"""
    victim = _spawn("import time\ntime.sleep(60)\n")
    time.sleep(0.3)
    image = anchor.process_image(victim.pid)
    ck("拿得到映像路径", bool(image), image)

    an = anchor.Anchor()
    ck("能认下活着的进程", an.attach(victim.pid, image))
    ck("认下之后判活为真", an.alive())
    ck("记的是它的 pid", an.pid == victim.pid)

    wrong = anchor.Anchor()
    ck("映像对不上就拒收（PID 复用防线）",
       not wrong.attach(victim.pid, r"C:\nope\other.exe"))
    ck("拒收之后没留下句柄", not wrong.attached)

    _kill(victim)
    ck("进程没了之后判活为假", not an.alive())
    an.detach()
    ck("放手之后不再持有句柄", not an.attached)
    ck("不存在的 pid 认不上", not anchor.Anchor().attach(999999, ""))


def check_note(ck: Checker, path: str) -> None:
    """纸条：写得进、读得出、没有就是没有。"""
    anchor.note_path = lambda: path
    if os.path.isfile(path):
        os.remove(path)
    ck("没纸条时读到 None", anchor.read_note() is None)
    ck("没纸条时状态行说的是没绑", "没绑" in anchor.status_line())

    ck("写得进", anchor.write_note(4242, r"C:\x\ChatGPT.exe"))
    ck("读得出原样", anchor.read_note() == (4242, r"C:\x\ChatGPT.exe"))
    ck("pid 非法就不写", anchor.write_note(0, "x") is False)

    before = os.stat(path).st_mtime_ns
    time.sleep(0.01)
    anchor.write_note(4242, r"C:\x\ChatGPT.exe")
    ck("内容没变就不动文件（不白刷 mtime）",
       os.stat(path).st_mtime_ns == before)

    ck("pid 已经不在了，状态行会说实话",
       "不在了" in anchor.status_line(), anchor.status_line())

    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    ck("纸条坏了当没有，不抛异常", anchor.read_note() is None)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write('{"pid": 0, "image": "x"}')
    ck("pid<=0 的纸条当没有", anchor.read_note() is None)
    os.remove(path)


def check_switch(ck: Checker, path: str) -> None:
    """换人：纸条变了跟着换；纸条没了就松手；认不出就不绑。"""
    anchor.note_path = lambda: path
    if os.path.isfile(path):
        os.remove(path)
    guard = _spawn("import time\ntime.sleep(60)\n")
    other = _spawn("import time\ntime.sleep(60)\n")
    time.sleep(0.3)
    g_img = anchor.process_image(guard.pid)

    w = supervise.Watcher()
    w.poll()
    ck("没纸条时不绑", not w.gone())

    anchor.write_note(guard.pid, g_img)
    w.poll()
    ck("纸条来了就绑上", w.pid() == guard.pid, str(w.pid()))
    ck("绑上之后它不是'已经没了'", not w.gone())

    w.poll()
    ck("纸条没变就不重复绑（pid 不变）", w.pid() == guard.pid)

    anchor.write_note(other.pid, anchor.process_image(other.pid))
    w.poll()
    ck("纸条换人了就跟着换", w.pid() == other.pid, str(w.pid()))

    anchor.write_note(999999, r"C:\x\ChatGPT.exe")
    w.poll()
    ck("认不出的人不绑（退回不绑，而不是乱绑）", not w.attached)

    anchor.write_note(guard.pid, g_img)
    w.poll()
    ck("重新认得出来就再绑上", w.pid() == guard.pid)

    os.remove(path)
    w.poll()
    ck("纸条没了就松手", not w.attached)
    ck("松手之后永不判'没了'（不绑=不管）", not w.gone())

    w.close()
    _kill(guard)
    _kill(other)


def check_quit(ck: Checker, path: str, tmpdir: str) -> None:
    """该收摊的时候收得掉：走优雅路径，宽限期过了才强制。"""
    anchor.note_path = lambda: path
    sentinel = os.path.join(tmpdir, "sentinel")
    if os.path.isfile(sentinel):
        os.remove(sentinel)
    os.environ["VP_SENTINEL"] = sentinel

    guard = _spawn("import time\ntime.sleep(60)\n")
    time.sleep(0.3)
    anchor.write_note(guard.pid, anchor.process_image(guard.pid))

    w = supervise.Watcher()
    w.poll()
    ck("先确认绑上了", w.pid() == guard.pid)

    # ① 锚点还在：不该动真身，也不该发收摊信号。
    child = _spawn(_SLEEPER)
    fake = FakeSingle(sentinel)
    real_single = supervise.single_instance
    supervise.single_instance = fake
    supervise.WATCH_POLL_SECONDS = 0.1
    try:
        w.poll()
        ck("锚点还活着时，不判'没了'", not w.gone())

        # ② 锚点没了：走优雅收摊，假真身收到哨兵后正常退出。
        _kill(guard)
        code, anchored_out = supervise._wait_child(child, w)
        ck("锚点没了会被判出来", anchored_out)
        ck("走的是优雅路径（发了收摊信号）", fake.quit_calls == 1,
           "quit_calls=%d" % fake.quit_calls)
        ck("真身正常退出（code 0，不是被杀）", code == 0, "code=%r" % code)
        ck("真身确实没了", child.poll() is not None)

        # ③ 假真身赖着不走：宽限期过了必须强制结束。
        stubborn = _spawn(_STUBBORN)
        fake2 = FakeSingle("")
        supervise.single_instance = fake2
        supervise.QUIT_GRACE_SECONDS = 0.5
        supervise.FORCE_GRACE_SECONDS = 10.0
        w2 = supervise.Watcher()
        anchor.write_note(999999, r"C:\x\ChatGPT.exe")   # 认不出的锚点
        w2.poll()
        ck("认不出的锚点不算'绑上了'", not w2.attached)

        guard2 = _spawn("import time\ntime.sleep(60)\n")
        time.sleep(0.3)
        anchor.write_note(guard2.pid, anchor.process_image(guard2.pid))
        w2.poll()
        started = time.monotonic()
        _kill(guard2)
        code2, out2 = supervise._wait_child(stubborn, w2)
        elapsed = time.monotonic() - started
        ck("赖着不走的会被强制结束", out2 and code2 != 0, "code=%r" % code2)
        ck("是等过宽限期才动手的（没提前杀）", elapsed >= 0.5,
           "elapsed=%.2f" % elapsed)
        ck("真身确实没了", stubborn.poll() is not None)
        ck("强制那次也发过收摊信号", fake2.quit_calls == 1,
           "quit_calls=%d" % fake2.quit_calls)
    finally:
        supervise.single_instance = real_single
        supervise.QUIT_GRACE_SECONDS = 180.0
        supervise.FORCE_GRACE_SECONDS = 10.0
        supervise.WATCH_POLL_SECONDS = 1.0
        w.close()
        for p in (child, guard2, stubborn):
            _kill(p)
        os.remove(path)


def check_unbound(ck: Checker, path: str, tmpdir: str) -> None:
    """没绑锚点时：真身自己退就走，不许乱发收摊信号。"""
    anchor.note_path = lambda: path
    if os.path.isfile(path):
        os.remove(path)
    child = _spawn("import sys, time\ntime.sleep(0.4)\nsys.exit(0)\n")
    fake = FakeSingle("")
    real_single = supervise.single_instance
    supervise.single_instance = fake
    supervise.WATCH_POLL_SECONDS = 0.1
    try:
        w = supervise.Watcher()
        w.poll()
        code, anchored_out = supervise._wait_child(child, w)
        ck("没锚点时真身自己退，如实返回", (code == 0 and not anchored_out),
           "code=%r out=%r" % (code, anchored_out))
        ck("没锚点时一次收摊信号都不发", fake.quit_calls == 0,
           "quit_calls=%d" % fake.quit_calls)
    finally:
        supervise.single_instance = real_single
        supervise.WATCH_POLL_SECONDS = 1.0
        _kill(child)


def main() -> int:
    ck = Checker()
    tmpdir = tempfile.mkdtemp(prefix="voicepill-sup-")
    note = os.path.join(tmpdir, "anchor.json")
    real_note_path = anchor.note_path
    print("=== 「和 Codex 共同启停」自测 ===")
    print("纸条（本次用临时文件）：%s" % note)
    try:
        for fn in (check_identify, check_handle):
            print("\n[%s]" % title(fn))
            fn(ck)
        print("\n[%s]" % "纸条：写得进、读得出、坏了当没有")
        check_note(ck, note)
        print("\n[%s]" % "换人：纸条变了跟着换")
        check_switch(ck, note)
        print("\n[%s]" % "收摊：优雅优先、宽限期兜底")
        check_quit(ck, note, tmpdir)
        print("\n[%s]" % "没绑锚点：不许乱动手")
        check_unbound(ck, note, tmpdir)
    finally:
        anchor.note_path = real_note_path
    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
