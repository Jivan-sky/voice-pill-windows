# -*- coding: utf-8 -*-
"""驻留看门狗：真身（main.py）要是崩了，把它拉起来。

为什么需要它
------------
「计划任务」自带失败后重启，本来是最好的方案（见 autostart.py）。但实测本机
**非管理员注册不了任何计划任务**（`Register-ScheduledTask` 与 `schtasks /create`
都是 Access is denied，四种写法全试过），只能退到启动文件夹快捷方式，而它
没有重启策略。

于是缺口变成：进程一旦硬崩（原生音频栈是最可能的地方），就一直不在，直到
下次登录——用户还以为是"今天没反应"。这个几十行的看门狗就是补这个缺口。

策略（每条都有理由）
------------------
* 孩子**正常退出（code 0）就一起退**。0 只可能来自 `--stop` 或 Ctrl+C，也就是
  用户明确要停——这时绝不能"你以为停了其实又起来了"。
* 非 0 退出 → 隔 5 秒拉一次。
* **连崩保护**：短时间内崩够 3 次就放弃并留一条日志。否则一个坏掉的配置
  （模型目录被移走之类）会变成每 5 秒一次的无限重启，机器被反复折腾，
  用户在任务管理器里还会看到进程一遍遍冒出来，比直接不工作更吓人。

看门狗自己也挂了怎么办：它没有复杂的依赖（不碰音频、不装钩子、不开 UI），
出错面只剩"拉起子进程"，所以不为它再加一层看门狗。

还要管「什么时候收摊」
----------------------
用户要的是「和 Codex 共同启停」。除了孩子，看门狗还盯一样东西：
`anchor.json` 里记着的那个 Codex 进程（SessionStart 钩子写下的，见
anchor.py）。它一退，看门狗就走 `--stop` 那条优雅路径请真身收摊，自己
跟着退。纸条读不到、或者认不出那个人（PID 被复用、是上一次 Codex 留下
的），就不绑——退回「常驻到天荒地老」的老行为。能力本身比绑生命周期重要，
绝不因为纸条靠不住就不干活。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import console                     # noqa: E402

console.make_output_safe()

import anchor                      # noqa: E402
import config                      # noqa: E402
import single_instance             # noqa: E402

RESTART_DELAY = 5.0            # 崩了之后等多久再拉
RAPID_WINDOW = 60.0            # "刚起来就崩"的判定窗口（秒）
RAPID_LIMIT = 3                # 窗口内崩够几次就放弃

# 锚点（见 anchor.py）：纸条上那个 Codex 进程一没，就请真身收摊。
WATCH_POLL_SECONDS = 1.0       # 盯子进程 / 看纸条的间隔
QUIT_GRACE_SECONDS = 180.0     # 请它收摊后最多等多久（最长录音 120 秒 + 转写余量）
FORCE_GRACE_SECONDS = 10.0     # terminate 之后再等多久，然后才 kill


def decide(rapid_failures: int) -> tuple:
    """纯逻辑，便于自测：返回 (动作, 延迟秒)。动作是 restart | give_up。"""
    if rapid_failures >= RAPID_LIMIT:
        return ("give_up", 0.0)
    return ("restart", RESTART_DELAY)


def pythonw_path() -> str:
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.isfile(exe) else sys.executable


def child_argv() -> list:
    return [pythonw_path(), os.path.join(config.project_root(), "src", "main.py")]


class Watcher:
    """照纸条盯住那个 Codex 进程。纸条变了就换人，读不到就当没锚点。

    为什么是「照纸条」而不是「启动时收一个 PID 参数」：开机自启那条路
    （autostart.py 的启动文件夹快捷方式）在登录时就把看门狗拉起来了，那会儿
    Codex 还没开。参数是一次性的，纸条可以改，所以看门狗每次轮询都重读一次。
    """

    def __init__(self) -> None:
        self._anchor = anchor.Anchor()
        self._note = None

    def poll(self) -> None:
        """看一眼纸条，需要就换锚点。出错只影响这一轮，不往上抛。"""
        try:
            note = anchor.read_note()
        except Exception:                              # noqa: BLE001
            return
        if note == self._note:
            return
        self._note = note
        if note is None:
            self._anchor.detach()
            return
        pid, image = note
        if self._anchor.attach(pid, image):
            print("[看门狗] 绑上 Codex：pid=%d（%s）"
                  % (pid, os.path.basename(image or "")), file=sys.stderr)
        else:
            print("[看门狗] 纸条上的 pid=%d 认不出来（多半是上一次 Codex 留下的），"
                  "这次不绑。" % pid, file=sys.stderr)

    def gone(self) -> bool:
        """锚点是不是没了。没绑锚点永远返回 False——不绑就等于不管。"""
        return self._anchor.attached and not self._anchor.alive()

    @property
    def attached(self) -> bool:
        return self._anchor.attached

    def pid(self) -> int:
        return self._anchor.pid

    def close(self) -> None:
        self._anchor.detach()


def kill_tree(pid: int) -> None:
    """连子孙一起结束。为什么必须整棵树，见 _force_stop 的注释。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _force_stop(proc) -> int:
    """强制结束真身，返回它的退出码。

    为什么不是 `proc.terminate()` 了事：`.venv\\Scripts\\pythonw.exe` 是个
    **跳板**——实测它自己会另外起一个 python.exe、然后等在那里（见 child_argv）。
    只 terminate 跳板，引擎还在跑，而看门狗会以为收工了：正是这个能力最该
    避免的「半死」。所以连子孙一起结束。
    """
    kill_tree(proc.pid)
    try:
        return proc.wait(timeout=FORCE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def _wait_child(proc, watcher) -> tuple:
    """盯着子进程，顺便盯着锚点。返回 (退出码, 是不是因为锚点没了才收的摊)。"""
    quit_at = None
    while True:
        try:
            # 已经因为锚点没了在收摊，那这次退出就得如实报出来——否则调用方
            # 只看到「code 0，正常退出」，会漏掉「是被 Codex 带走的」这件事。
            return (proc.wait(timeout=WATCH_POLL_SECONDS), quit_at is not None)
        except subprocess.TimeoutExpired:
            pass
        if quit_at is None:
            watcher.poll()
            if not watcher.gone():
                continue
            # 走 `--stop` 那条路，而不是强杀：真身要收尾（拔钩子、写日志尾）。
            quit_at = time.monotonic() + QUIT_GRACE_SECONDS
            single_instance.request_quit()
            print("[看门狗] Codex（pid=%d）退了，已请驻留实例收摊（最多等 %.0f 秒）。"
                  % (watcher.pid(), QUIT_GRACE_SECONDS), file=sys.stderr)
            continue
        if time.monotonic() < quit_at:
            continue
        # 到这儿多半是正卡在录音/转写里。宽限期已经给了，别再无限等。
        print("[看门狗] 等了 %.0f 秒还没收完（多半卡在录音或转写），强制结束。"
              % QUIT_GRACE_SECONDS, file=sys.stderr)
        return (_force_stop(proc), True)


def main() -> int:
    console.attach_app_log(config.app_log_path(), label="看门狗启动")

    lock = single_instance.InstanceLock(single_instance.SUPERVISOR_MUTEX_NAME)
    try:
        if not lock.acquire():
            print("[看门狗] 已经有一只在看守了，本进程退出。", file=sys.stderr)
            return 1
    except OSError as exc:
        print("[看门狗] 拿不到自己的互斥体：%s" % exc, file=sys.stderr)
        return 1

    watcher = Watcher()
    code = 0
    try:
        rapid = 0
        while True:
            t0 = time.monotonic()
            try:
                proc = subprocess.Popen(child_argv(),
                                        cwd=config.project_root())
            except OSError as exc:
                print("[看门狗] 拉不起子进程：%s" % exc, file=sys.stderr)
                return 1
            # KeyboardInterrupt 不该把子进程丢下不管
            try:
                code, anchored_out = _wait_child(proc, watcher)
            except KeyboardInterrupt:
                code = proc.wait()
                anchored_out = False
            alive = time.monotonic() - t0

            if anchored_out:
                print("[看门狗] Codex 退了，一起退。")
                return 0
            if code == 0:
                print("[看门狗] 子进程正常退出（code 0），一起退。")
                return 0

            rapid = rapid + 1 if alive < RAPID_WINDOW else 1
            action, delay = decide(rapid)
            print("[看门狗] 子进程退出 code=%s，活了 %.1f 秒（连崩 %d 次）。"
                  % (code, alive, rapid), file=sys.stderr)
            if action == "give_up":
                print("[看门狗] 短时间连崩 %d 次，不再拉。多半是配置或模型坏了，"
                      "先 `main.py --check`。" % rapid, file=sys.stderr)
                return 1
            time.sleep(delay)
    finally:
        watcher.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
