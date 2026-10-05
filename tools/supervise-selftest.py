# -*- coding: utf-8 -*-
"""「和 Codex 共同启停」自测：认人、句柄、纸条、换人、接管、收摊、兜底。

为什么需要它
------------
这条能力最难查的坏法全是**静默**的：锚点认错人 → 关掉别的程序把语音一起
带走；PID 复用没防住 → 半天之后突然自己退了；纸条读不出来 → 绑不上却看不出
来（退回常驻，用户以为绑上了）。所以这里不测「正常路径通不通」，测的是
**认人的边界**和**该收摊的时候真收得掉**。

「接管」与「纸条纪律」这两档是 2026-10-06 补的，起因是用户看到的**一直弹黑窗**：
看门狗每一轮都另拉一只驻留、驻留每次都认出「已经有一个在跑」、看门狗把那个 code 3
当崩溃又重拉——六秒里连起三只，每只都冒一个可见的控制台窗口。所以这里锁三件事：
驻留在服务时**本只不许另拉进程**（不冒第二个窗）、认出「抢先跑起来了」就收摊且
**不重拉**、以及**什么时候才该留停用纸条**（写多写少都会害到哨兵，见下）。

全程不碰真身：纸条指向一个临时文件，收摊信号打在假的 single_instance 上。

用法：
    .venv\\Scripts\\python.exe tools\\supervise-selftest.py
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import anchor                      # noqa: E402
import single_instance             # noqa: E402
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

# 一个「已经有一只驻留在服务」的假驻留：拿着单实例锁不放。
# 时序全部由文件定：拿上锁写 ready 报信，等 release 出现再松手 —— 不靠 sleep 猜，
# 免得「探到了但刚好它已经走了」这种假失败。
#   argv[1]=src 目录  argv[2]=锁名  argv[3]=ready 文件  argv[4]=release 文件
_HOLDER = (
    "import os, sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import single_instance\n"
    "lock = single_instance.InstanceLock(sys.argv[2])\n"
    "if not lock.acquire():\n"
    "    sys.exit(9)\n"
    "handle = open(sys.argv[3], 'w')\n"
    "handle.write('ready')\n"
    "handle.close()\n"
    "end = time.monotonic() + 60\n"
    "while not os.path.exists(sys.argv[4]) and time.monotonic() < end:\n"
    "    time.sleep(0.02)\n"
    "lock.release()\n"
)


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


def _spawn(code: str, *args):
    return subprocess.Popen([sys.executable, "-c", code, *args])


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


class _CaptureOtherThreads(io.StringIO):
    """除「装它的那个线程」以外，别的线程写进来的都收进缓冲区。

    为什么要按线程分：contextlib.redirect_stdout 换的是**进程级** sys.stdout。接管
    那一档 main() 跑在别的线程里，主线程这时候打的报告会被一起吞掉 —— 2026-10-06
    实测踩到：两条断言的结果人间蒸发，报告看起来还是"全过"。装它的那个线程（主线程）
    照常转给原来的 stdout，报告要看得见；被测那个线程的输出收进缓冲区备用。
    """

    def __init__(self, keep) -> None:
        io.StringIO.__init__(self)
        self._keep = keep
        self._owner = threading.get_ident()

    def write(self, text):
        if threading.get_ident() == self._owner:
            return self._keep.write(text)
        return io.StringIO.write(self, text)

    def flush(self) -> None:
        self._keep.flush()


def _run_main(tmpdir: str, child_code: str, resident_mutex: str,
                 record: dict = None, poll: float = 0.05,
                 quiet: bool = True) -> dict:
    """在假环境里**真跑一次** supervise.main()，把下场记进 record 里。

    为什么要真跑 main()：这一轮要锁的三件事（接管、code 3 收摊、留不留停用纸条）
    分别落在主循环和 finally 里。把它们拆成几个小函数来测，测的是「我重写的那个
    版本」，不是产品里真跑的那条路——而这次踩到的两个坑（拿 property 当方法用、
    每轮多拉一只驻留冒一个窗）恰恰都长在那条真路上。

    只换边界，不换被测的东西：
      * 互斥体名字加测试后缀 —— 绝不和本机真在跑的那把撞名；
      * 纸条指向临时文件（不存在 = 没绑锚点，看门狗不会去请谁收摊）；
      * 拉起的子进程换成一小段 python（子进程说 code 3 / code 0 由调用方给）；
      * 写停用纸条、发收摊信号、写 app.log 全部换成记账。

    quiet=False 只在「main() 跑在别的线程」时用：那种场合得让调用方拿
    _CaptureOtherThreads 去收，进程级重定向会把调用方的报告一起吞掉。
    **不碰**真驻留、真看门狗、真 app.log、真 stopped.json（那张纸写下去会拦住哨兵）。
    """
    if record is None:
        record = {}
    record.setdefault("spawns", 0)
    record.setdefault("stop_notes", [])
    record.setdefault("quit_calls", 0)
    note = os.path.join(tmpdir, "run-anchor-%d.json" % os.getpid())
    if os.path.isfile(note):
        os.remove(note)

    saved = {
        "child_argv": supervise.child_argv,
        "attach_app_log": console.attach_app_log,
        "write_stop_note": anchor.write_stop_note,
        "note_path": anchor.note_path,
        "request_quit": single_instance.request_quit,
        "resident_mutex": single_instance.MUTEX_NAME,
        "supervisor_mutex": single_instance.SUPERVISOR_MUTEX_NAME,
        "watch_poll": supervise.WATCH_POLL_SECONDS,
        "restart_delay": supervise.RESTART_DELAY,
    }

    def fake_argv():
        record["spawns"] += 1
        return [sys.executable, "-c", child_code]

    def fake_quit():
        record["quit_calls"] += 1
        return True

    def fake_stop_note(pid):
        record["stop_notes"].append(pid)
        return True

    supervise.child_argv = fake_argv
    console.attach_app_log = lambda *a, **k: None
    anchor.write_stop_note = fake_stop_note
    anchor.note_path = lambda: note
    single_instance.request_quit = fake_quit
    single_instance.MUTEX_NAME = resident_mutex
    single_instance.SUPERVISOR_MUTEX_NAME = resident_mutex + ".supervisor"
    supervise.WATCH_POLL_SECONDS = poll
    supervise.RESTART_DELAY = poll

    buf = io.StringIO()
    try:
        if quiet:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                record["code"] = supervise.main()
        else:
            record["code"] = supervise.main()
    finally:
        supervise.child_argv = saved["child_argv"]
        console.attach_app_log = saved["attach_app_log"]
        anchor.write_stop_note = saved["write_stop_note"]
        anchor.note_path = saved["note_path"]
        single_instance.request_quit = saved["request_quit"]
        single_instance.MUTEX_NAME = saved["resident_mutex"]
        single_instance.SUPERVISOR_MUTEX_NAME = saved["supervisor_mutex"]
        supervise.WATCH_POLL_SECONDS = saved["watch_poll"]
        supervise.RESTART_DELAY = saved["restart_delay"]
    record["output"] = buf.getvalue()
    return record


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


def check_takeover(ck: Checker, path: str, tmpdir: str) -> None:
    """接管：已经有一只驻留在服务 —— 本只只看着，不另拉进程、它走才收。"""
    anchor.note_path = lambda: path
    if os.path.isfile(path):
        os.remove(path)

    name = "Local\\VoicePill.Test.Takeover.%d" % os.getpid()
    ready = os.path.join(tmpdir, "takeover-ready")
    release = os.path.join(tmpdir, "takeover-release")
    for item in (ready, release):
        if os.path.isfile(item):
            os.remove(item)
    src_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src")

    holder = _spawn(_HOLDER, src_dir, name, ready, release)
    probe = single_instance.InstanceLock(name)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not os.path.isfile(ready):
            time.sleep(0.02)
        ck("假驻留起来了，并且把锁拿上了", os.path.isfile(ready))
        ck("探得到**另一个进程**拿着的驻留锁",
           supervise.resident_alive(probe), "别人持锁却探不到")
        ck("没人在拿的那把，探到的是『没有』",
           not supervise.resident_alive(
               single_instance.InstanceLock(name + ".free")))

        box = {}
        sink = _CaptureOtherThreads(sys.stdout)
        real_stdout, real_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = sink, sink
        try:
            thread = threading.Thread(
                target=lambda: _run_main(tmpdir, "import sys; sys.exit(0)", name,
                                         box, quiet=False),
                daemon=True)
            thread.start()
            time.sleep(0.6)
            ck("驻留在服务时，本只没另拉进程（不冒第二个窗、不空转）",
               box.get("spawns") == 0, "spawns=%r" % box.get("spawns"))
            ck("驻留在服务时，本只还守着、没收摊", "code" not in box)
            with io.open(release, "w") as fh:
                fh.write("go")
            thread.join(timeout=20)
        finally:
            sys.stdout, sys.stderr = real_stdout, real_stderr
        box["output"] = sink.getvalue()
        ck("日志里说清了是『接管看守』", "接管" in box["output"],
           box["output"][-160:])
        ck("驻留走了就一起收摊", box.get("code") == 0, "code=%r" % box.get("code"))
        ck("接管档收摊时没乱发收摊信号（锚点没绑）", box.get("quit_calls") == 0,
           "quit_calls=%r" % box.get("quit_calls"))
    finally:
        if not os.path.isfile(release):
            with io.open(release, "w") as fh:
                fh.write("go")
        _kill(holder)


def check_stand_down(ck: Checker, path: str, tmpdir: str) -> None:
    """认出「已经有一个实例在跑」（code 3）：收摊、不重拉、也不留停用纸条。"""
    anchor.note_path = lambda: path
    if os.path.isfile(path):
        os.remove(path)
    name = "Local\\VoicePill.Test.StandDown.%d" % os.getpid()
    rec = _run_main(tmpdir, "import sys; sys.exit(3)", name)
    ck("只拉了一次（认出有人在跑就不再试）", rec["spawns"] == 1,
       "spawns=%r" % rec["spawns"])
    ck("本只跟着收摊，不算失败", rec["code"] == 0, "code=%r" % rec["code"])
    ck("这一档不留停用纸条（写了哨兵会被误拦）", rec["stop_notes"] == [],
       "stop_notes=%r" % rec["stop_notes"])
    ck("日志里说清了是『抢先跑起来了』", "抢先" in rec["output"],
       rec["output"][-160:])


def check_stop_note(ck: Checker, path: str, tmpdir: str) -> None:
    """用户明确停（子进程 code 0）：留一张停用纸条 —— 哨兵靠它不偷偷拉回来。"""
    anchor.note_path = lambda: path
    if os.path.isfile(path):
        os.remove(path)
    name = "Local\\VoicePill.Test.UserStop.%d" % os.getpid()
    rec = _run_main(tmpdir, "import sys; sys.exit(0)", name)
    ck("正常退出不重拉", rec["spawns"] == 1, "spawns=%r" % rec["spawns"])
    ck("本只跟着收摊", rec["code"] == 0, "code=%r" % rec["code"])
    ck("留了一张停用纸条（哨兵据此不补拉）", len(rec["stop_notes"]) == 1,
       "stop_notes=%r" % rec["stop_notes"])


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
        print("\n[%s]" % "接管：已经有一只驻留在服务，本只只看着")
        check_takeover(ck, note, tmpdir)
        print("\n[%s]" % "认出『已经有一个在跑』：收摊、不重拉、不留纸条")
        check_stand_down(ck, note, tmpdir)
        print("\n[%s]" % "用户明确停：留一张纸条，哨兵别偷偷拉回来")
        check_stop_note(ck, note, tmpdir)
    finally:
        anchor.note_path = real_note_path
    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
