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

# 驻留认出「已经有一个实例在跑」时的退出码（见 src/main.py 那一段）。对看门狗来说
# 这不是崩溃，是「有人比我先到」——重试没有意义。
EXIT_ALREADY_RUNNING = 3


def resident_alive(resident) -> bool:
    """已经有一个驻留实例在服务没有——探它那把单实例锁。问不出来就当没有。

    探的是"**有没有人**拿着"，不是"我拿没拿到"：看门狗和驻留是两个进程，锁在驻留
    手上，"自己的持锁状态"永远问不出东西来。2026-10-06 实测踩到：写成了
    resident.held()，而 held 是 property —— 直接 TypeError，看门狗一起来就崩，
    连驻留都拉不起来了。InstanceLock.exists 用 OpenMutex 纯探测，不抢所有权。

    问不出来时返回 False 是有意选的方向：看门狗照常拉一只驻留，那一只会自己认出
    "已经有一个在跑"并以 code 3 退出，本只随即收摊（见 main 里那一档）。代价是白起
    一次进程，换来的是**绝不会**因为探错而让能力没人管。
    """
    if resident is None:
        return False
    try:
        return resident.exists()
    except OSError:
        return False


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


def _wait_child(proc, watcher, resident=None) -> tuple:
    """盯着子进程，顺便盯着锚点。返回 (退出码, 是不是因为锚点没了才收的摊)。

    `proc=None` 表示这一轮本只**没有**子进程：已经有一只驻留在服务，本只只接管看守
    （见 main）。那时拿驻留锁当替身——锁一松就是它走了。
    """
    quit_at = None
    while True:
        if proc is None:
            if not resident_alive(resident):
                return (0, quit_at is not None)
            time.sleep(WATCH_POLL_SECONDS)
        else:
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
        if proc is None:
            # 接管来的那只不是自己的孩子，没有句柄可强杀；如实收摊，别赖着。
            print("[看门狗] 接管的那只驻留等了 %.0f 秒还没收完，本只退出。"
                  % QUIT_GRACE_SECONDS, file=sys.stderr)
            return (0, True)
        # 到这儿多半是正卡在录音/转写里。宽限期已经给了，别再无限等。
        print("[看门狗] 等了 %.0f 秒还没收完（多半卡在录音或转写），强制结束。"
              % QUIT_GRACE_SECONDS, file=sys.stderr)
        return (_force_stop(proc), True)


def main() -> int:
    # 横幅先打：每只进程都要留下「我来过」这条痕。抢不到锁的那只以前只在 stderr
    # 说话，而 warm-up 拉起它时 stderr 指向 devnull —— 判决消失，日志里只剩一条没有
    # 下文的「看门狗启动」（2026-10-05 实测踩到：四次 spawn 只留下两条横幅，另两条
    # 连 Python 启动都没跑完）。现在两种下场都落在 app.log 上。
    console.attach_app_log(config.app_log_path(), label="看门狗进程起")

    lock = single_instance.InstanceLock(single_instance.SUPERVISOR_MUTEX_NAME)
    try:
        if not lock.acquire():
            print("[看门狗] 已经有一只在看守了，本进程退出。")
            return 1
    except OSError as exc:
        print("[看门狗] 拿不到自己的互斥体：%s" % exc)
        return 1
    print("[看门狗] 互斥体到手，开始看守。")

    watcher = Watcher()
    code = 0
    try:
        rapid = 0
        # 留不留那张停用纸条（哨兵据此判断"这一代 Codex 用户明确停过，不许偷偷拉回来"）。
        # **只有"用户明确要停"那一档才置位**——就是子进程正常退出（code 0，来自
        # `--stop` 或 Ctrl+C）那一次。2026-10-06 实测的坑：以前是"除了 code 3 那一档
        # 全都写"，于是**连崩到放弃**也写了一张。哨兵读到它就不敢补拉，用户修好配置
        # 之后按 Fn 依然没反应，一直哑到下一次 Codex 会话——而那一档恰恰最该重试。
        user_stopped = False
        resident = single_instance.InstanceLock(single_instance.MUTEX_NAME)
        while True:
            t0 = time.monotonic()
            if resident_alive(resident):
                # 已经有一只驻留在服务：**接管看守**，不再另拉一只。拉了它也会以
                # code 3（已有实例）退出去，等于每轮白起一个进程（2026-10-06 实测：
                # 六秒里连起三只，每只都冒一次窗口）。盯锚点这件事一样做到：它一退，
                # 下面那段照样请那只驻留收摊。
                print("[看门狗] 已经有一个驻留实例在服务，本只接管看守。")
                proc = None
            else:
                try:
                    # 必须带 CREATE_NO_WINDOW。看门狗自己是被 DETACHED 拉起来的（没有
                    # 控制台），不显式关掉的话 Windows 只能给子进程**新建一个控制台**——
                    # 那是一个看得见的窗口。2026-10-06 实测（照这条链复刻，只差 flags）：
                    # 不传 flags 时新窗口 vis=True，传 CREATE_NO_WINDOW 时 vis=False。
                    # 而关掉那个窗口会让驻留收到 CTRL_CLOSE_EVENT、以 0xC000013A 退出，
                    # 看门狗再拉一个、窗口又冒出来——用户看到的「一直弹窗」就是这个环。
                    # 另：venv 里的 pythonw.exe 与 python.exe 逐字节相同（uv 造的跳板，
                    # 见 _force_stop 的注释），它拉起的就是控制台的 python.exe，所以这条
                    # 路上「pythonw = 无窗口」这个假设本来也不成立。
                    proc = subprocess.Popen(
                        child_argv(),
                        cwd=config.project_root(),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError as exc:
                    print("[看门狗] 拉不起子进程：%s" % exc, file=sys.stderr)
                    return 1
            # KeyboardInterrupt 不该把子进程丢下不管
            try:
                code, anchored_out = _wait_child(proc, watcher, resident)
            except KeyboardInterrupt:
                code = proc.wait() if proc is not None else 0
                anchored_out = False
            alive = time.monotonic() - t0

            if anchored_out:
                print("[看门狗] Codex 退了，一起退。")
                return 0
            if proc is None:
                # 接管的那只走了（锁松了）。本只不再自作主张往下拉——留给外面那条
                # 路去决定（工具调用 / 预热 / 哨兵），免得跟 `--stop` 对着干。
                print("[看门狗] 接管的那只驻留走了，一起退。")
                return 0
            if code == 0:
                print("[看门狗] 子进程正常退出（code 0），一起退。")
                user_stopped = True
                return 0
            if code == EXIT_ALREADY_RUNNING:
                # 有一只比我先到的驻留在跑（并发拉起的竞态）。这不是崩溃，重试没有
                # 意义：主动退出，让跑到前面的那只服务。**不留停用纸条**——那张纸的
                # 意思是「用户明确停过」，写错了哨兵就不敢补拉（见 finally 的注释）。
                print("[看门狗] 已经有一个驻留实例抢先跑起来了（code 3），本只退出。")
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
        # 只有"用户明确要停"（子进程 code 0）那一档留停用纸条：哨兵靠它把「用户的
        # 决定」和「崩了 / 被外力打死」分开——纸条在，哨兵就不补拉（见
        # engine.StoppedByUser 的两张纸先后判据）。
        #
        # 为什么别的下场都不留：**留错比不留危险得多**。看门狗崩了、被人杀了、
        # 连崩到放弃、认出"已经有一只驻留在跑"——这些都不是用户的决定，写一张纸
        # 就够让哨兵从此不敢补拉，用户按 Fn 没反应却查不出原因（2026-10-06 实测
        # 就这么哑过一轮）。不留最多多补拉一次，代价只是多跑一次 EnsureSupervisor。
        #
        # pid 要在 close() 之前取：close() 一断锚点，pid 就归 0 了。
        if user_stopped:
            anchor.write_stop_note(watcher.pid())
        watcher.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
