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
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import console                     # noqa: E402

console.make_output_safe()

import config                      # noqa: E402
import single_instance             # noqa: E402

RESTART_DELAY = 5.0            # 崩了之后等多久再拉
RAPID_WINDOW = 60.0            # "刚起来就崩"的判定窗口（秒）
RAPID_LIMIT = 3                # 窗口内崩够几次就放弃


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
                code = proc.wait()
            except KeyboardInterrupt:
                code = proc.wait()
            alive = time.monotonic() - t0

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
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
