# -*- coding: utf-8 -*-
"""常驻形态自测：单实例锁、录音护栏、数据清理、日志轮转、--stop、配置热重载。

为什么需要它
------------
"按 Fn 说话 → 出字"这条主链路已经有 pipe-selftest（管道）和 capture-probe
（采音）两把尺子。但这一轮新加的整套**外壳**全是没有界面的行为：单实例、
无窗口、开机自启、日志、清理、退出信号。它们坏掉时没有任何可见症状——用户
只会觉得"它有时候不灵"，而"有时候"是最难查的那类故障。

本脚本不按 Fn、不碰麦克风、不需要人：起一个**真的** pythonw 驻留进程，
然后从外面观察它该做的事有没有发生。

用法：
    .venv\\Scripts\\python.exe tools\\resident-selftest.py

副作用与保护
------------
* 会临时改 `settings.json`，**结束后原样还原**（含"原本不存在"的情形）。
* 会真的起一个 pythonw 驻留进程，结束时保证它已经退出。
* 检测到已有实例在跑就**直接拒绝执行**——绝不打扰你正在用的那一个。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import autostart                   # noqa: E402
import config                      # noqa: E402
import main as app_main            # noqa: E402
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


def tail_since(path: str, offset: int) -> str:
    """读日志里 offset 之后的新增内容。识别"这次发生了没有"，不看旧账。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() <= offset:
                return ""
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def log_size(path: str) -> int:
    return os.path.getsize(path) if os.path.isfile(path) else 0


def wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return bool(pred())


def app_lock_held() -> bool:
    """应用的单实例锁现在有没有被别人握着（= 驻留实例在跑）。"""
    probe = single_instance.InstanceLock()
    if probe.acquire():
        probe.release()
        return False
    return True


def supervisor_lock_held() -> bool:
    """看门狗的互斥体现在有没有被别人握着。"""
    probe = single_instance.InstanceLock(single_instance.SUPERVISOR_MUTEX_NAME)
    if probe.acquire():
        probe.release()
        return False
    return True


def snapshot_settings():
    path = config.Settings.path()
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def restore_settings(text) -> None:
    path = config.Settings.path()
    if text is None:
        if os.path.isfile(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------- [A] 单实例锁 ----------

def check_instance_lock(ck: Checker) -> None:
    print("\n[A] 单实例锁（两个实例会各粘一遍，必须挡住）")
    name = "Local\\VoicePill.Selftest.%d" % os.getpid()
    first = single_instance.InstanceLock(name)
    second = single_instance.InstanceLock(name)
    ck("第一次 acquire 成功", first.acquire())
    ck("第二次 acquire 被拒", not second.acquire())
    first.release()
    third = single_instance.InstanceLock(name)
    ck("释放后能重新拿到（不会留下死锁）", third.acquire())
    third.release()


# ---------- [B] 录音时长护栏 ----------

def check_watchdog(ck: Checker) -> None:
    print("\n[B] 录音时长护栏（keyup 丢了不能一直录下去）")
    fired = []
    dog = app_main.RecordWatchdog(0.2, lambda: fired.append(1))
    dog.arm()
    time.sleep(0.6)
    ck("到点触发一次", fired == [1] and dog.fired)
    dog.arm()
    time.sleep(0.6)
    ck("重新武装后能再触发", len(fired) == 2)
    dog.arm()
    dog.disarm()
    time.sleep(0.4)
    ck("disarm 之后不再触发（正常松手的路径）", len(fired) == 2)

    disabled = []
    app_main.RecordWatchdog(0, lambda: disabled.append(1)).arm()
    time.sleep(0.3)
    ck("上限 0 = 不限（不武装）", not disabled)


# ---------- [C] 日志轮转 ----------

def check_rotation(ck: Checker) -> None:
    print("\n[C] 应用日志轮转（驻留几个月不能长成一个巨型文件）")
    path = os.path.join(tempfile.gettempdir(), "vp-rotate-%d.log" % os.getpid())
    try:
        for _ in range(3):
            with open(path, "ab") as fh:
                fh.write(b"a" * 2048)
            console.rotate_if_needed(path, max_bytes=1024, keep=2)
        # 轮转是"改名让位"，不是截断：旧内容整体搬到 .1，当前文件消失，
        # 下一次写日志时自然重建（和 logrotate 一个思路）。
        ck("滚动后原文件已让位", not os.path.exists(path))
        ck("生成了 .1", os.path.isfile(path + ".1"))
        ck("生成了 .2", os.path.isfile(path + ".2"))
        ck("keep=2 之外的不留（没有 .3）", not os.path.isfile(path + ".3"))
        with open(path, "ab") as fh:
            fh.write(b"b")
        ck("下一次写入重新建出当前文件",
           os.path.isfile(path) and os.path.getsize(path) == 1)
    finally:
        for suffix in ("", ".1", ".2", ".3"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


# ---------- [D] 过期数据清理 ----------

def check_prune(ck: Checker) -> None:
    print("\n[D] 过期留存清理（否则每次失败都留一个文件，只增不减）")
    old_wav = os.path.join(config.recordings_dir(), "rec-20000101-000000.wav")
    old_log = os.path.join(config.logs_dir(), "rec-20000101-000000.log")
    fresh = os.path.join(config.recordings_dir(), "rec-20990101-000000.wav")
    app_log = config.app_log_path()
    # 本脚本自己挂了日志（见 main()），所以 app.log 必然存在；仍然做个存在性
    # 判断，是为了这段逻辑单独拿出来跑时不会假失败。
    app_log_mtime = os.path.getmtime(app_log) if os.path.isfile(app_log) else 0.0

    try:
        for path in (old_wav, old_log, fresh):
            with open(path, "wb") as fh:
                fh.write(b"x")
        stamp = time.time() - 30 * 86400
        os.utime(old_wav, (stamp, stamp))
        os.utime(old_log, (stamp, stamp))
        if app_log_mtime:
            # 只改时间戳，不动内容：验证 app.log 不在清理范围内
            os.utime(app_log, (stamp, stamp))

        removed = config.prune_data(7)
        ck("30 天前的失败录音被删", not os.path.exists(old_wav))
        ck("30 天前的转写日志被删", not os.path.exists(old_log))
        ck("今天的录音留着", os.path.exists(fresh))
        ck("app.log 不参与清理（进程正开着它）",
           (app_log_mtime == 0.0 or os.path.isfile(app_log))
           and app_log not in removed)
    finally:
        for path in (old_wav, old_log, fresh):
            try:
                os.remove(path)
            except OSError:
                pass
        if app_log_mtime:
            try:
                os.utime(app_log, (app_log_mtime, app_log_mtime))
            except OSError:
                pass


# ---------- [E] 驻留端到端 ----------

def check_resident(ck: Checker) -> None:
    print("\n[E] 驻留端到端（按自启的真实形态：看门狗 + 真身，从外面观察）")
    pythonw = autostart.pythonw_path()
    entry = autostart.entry_path()
    guard = autostart.guard_path()
    log = config.app_log_path()
    ck("pythonw 可用（无窗口的启动器）",
       os.path.basename(pythonw).lower() == "pythonw.exe", pythonw)

    offset = log_size(log)
    proc = subprocess.Popen([pythonw, guard], cwd=config.project_root())
    try:
        ck("看门狗起来了（自己的互斥体被占）",
           wait_until(supervisor_lock_held, 25), "25 秒内没起来，看 %s" % log)
        ck("看门狗把真身拉起来了（单实例锁被占）",
           wait_until(app_lock_held, 25))
        tail = tail_since(log, offset)
        ck("应用日志出现启动横幅（说明无控制台时日志接管生效）",
           "Voice Pill 已启动" in tail, tail[-400:])

        dup = subprocess.run([sys.executable, entry],
                             cwd=config.project_root(),
                             capture_output=True, timeout=60)
        ck("第二个实例被拒（exit code 3）", dup.returncode == 3,
           "实际 %d" % dup.returncode)
        ck("拒绝时说清了原因",
           "已经有一个" in dup.stderr.decode("utf-8", "replace"))

        # 热重载：改 settings.json（配置项换一半的写法不重要，只求变），
        # 8 秒内没反应就再改一次——躲开"驻留实例还没记下初始 mtime"的窗口。
        probe = config.Settings.load()
        probe.auto_paste = not probe.auto_paste
        probe.save()
        reloaded = wait_until(
            lambda: "[配置] 已重载" in tail_since(log, offset), 8)
        if not reloaded:
            probe.auto_paste = not probe.auto_paste
            probe.save()
            reloaded = wait_until(
                lambda: "[配置] 已重载" in tail_since(log, offset), 10)
        ck("改 settings.json 后热重载（不用重启驻留进程）", reloaded,
           tail_since(log, offset)[-400:])

        stopped = subprocess.run([sys.executable, entry, "--stop"],
                                 cwd=config.project_root(),
                                 capture_output=True, timeout=60)
        ck("--stop 返回 0", stopped.returncode == 0,
           stopped.stderr.decode("utf-8", "replace")[-200:])
        ck("真身自己退出了", wait_until(lambda: not app_lock_held(), 25),
           "25 秒没退，可能卡在收摊")
        ck("日志有配对的收尾行（可据此判断崩溃）",
           "=== 退出" in tail_since(log, offset)
           and "code=0" in tail_since(log, offset))
        ck("看门狗没有把停掉的真身又拉起来（跟着退了）",
           wait_until(lambda: proc.poll() is not None, 15),
           "看门狗还在跑 = --stop 之后又被拉了一遍")
        ck("看门狗退出码 0", proc.poll() == 0, "实际 %s" % proc.poll())
        ck("看门狗也放开了自己的互斥体",
           wait_until(lambda: not supervisor_lock_held(), 5))
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


# ---------- [G] 看门狗的重启策略 ----------

def check_supervisor_logic(ck: Checker) -> None:
    print("\n[G] 看门狗重启策略（无限重启比不工作更折腾人）")
    ck("第 1 次崩溃 → 重拉", supervise.decide(1)[0] == "restart")
    ck("第 2 次崩溃 → 还拉", supervise.decide(2)[0] == "restart")
    ck("短时间崩到第 3 次 → 放弃", supervise.decide(3)[0] == "give_up")
    ck("重拉之间有延迟（不是空转）", supervise.decide(1)[1] > 0)
    # "正常退出就一起退"这条由 [E] 段实证（--stop 之后看门狗没把真身拉回来）


# ---------- [F] 自启计划（只算不装）----------

def check_autostart_plan(ck: Checker) -> None:
    print("\n[F] 开机自启的安装计划（本自测**只算不装**）")
    plan = autostart.plan()
    ck("可执行文件存在", os.path.isfile(plan["exe"]), plan["exe"])
    ck("入口文件存在", os.path.isfile(autostart.entry_path()),
       autostart.entry_path())
    ck("工作目录正确", os.path.isdir(plan["cwd"]), plan["cwd"])
    ck("自启挂的是看门狗而不是真身", "supervise.py" in plan["arg"],
       plan["arg"])
    print("  %s" % autostart.describe().replace("\n", "\n  "))


def main() -> int:
    print("=" * 68)
    print("Voice Pill 常驻形态自测")
    print("=" * 68)

    # 本脚本也挂日志：一来它的输出该和驻留进程记在同一本账里（排查时同一条
    # 时间线），二来 [D] 段要验"app.log 不被清理"，得先确保它真的存在。
    console.attach_app_log(config.app_log_path(), label="resident-selftest")

    if app_lock_held():
        print("⚠️  已有实例在跑（单实例锁被占着）。")
        print("    本自测需要独占，先 `python src/main.py --stop` 再跑。")
        return 2

    ck = Checker()
    saved = snapshot_settings()
    try:
        # 驻留测试期间关掉悬浮条：不需要看，也避免在屏幕上闪一个窗口
        probe = config.Settings.load()
        probe.hud_enabled = False
        probe.save()

        # 一段炸了不该让后面的段看不到结果：诊断工具最忌讳"第一个错就闭嘴"
        for label, fn in (("[A]", check_instance_lock),
                          ("[B]", check_watchdog),
                          ("[C]", check_rotation),
                          ("[D]", check_prune),
                          ("[E]", check_resident),
                          ("[F]", check_autostart_plan),
                          ("[G]", check_supervisor_logic)):
            try:
                fn(ck)
            except Exception as exc:               # noqa: BLE001
                ck("%s 段自身抛异常：%r" % (label, exc), False)
    finally:
        restore_settings(saved)
        print("\nsettings.json 已还原。")

    print("=" * 68)
    if ck.failed:
        print("❌ %d 项没过" % ck.failed)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
