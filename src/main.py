# -*- coding: utf-8 -*-
"""Voice Pill for Windows —— 入口与状态机。

按住主键说话，松手把文字粘到开始录音时所在的窗口。

架构与原版一一对应（见 docs/移植方案.md）：
    hotkey.py        热键     ←→  VoicePill.swift 的 NSEvent 监听
    audio.py         采音     ←→  LiveTranscriber.swift
    live_session.py  管道     ←→  LiveTranscriptionSession.swift
    providers.py     后端表   ←→  LiveProtocol.swift
    paste.py         粘贴     ←→  PasteController.swift
    hud.py           字幕条   ←→  HUD.swift

用法：
    python main.py                  正常启动
    python main.py --check          自检：引擎、麦克风、配置
    python main.py --list-devices   列麦克风
    python main.py --once           录一次就退出（调试用）

常驻形态
--------
本工具的目标形态是「一直在后台待命」：开机自己起来、没有窗口、按键即用。
为此多出这几个开关：
    main.py --install-autostart     开机自启（优先计划任务，见 autostart.py）
    main.py --uninstall-autostart   取消开机自启
    main.py --stop                  让正在驻留的那个实例优雅退出
    main.py --retry                 重试留存的失败录音（尚未实现）

驻留时用 `pythonw.exe` 启动，没有控制台；所有输出进
`%LOCALAPPDATA%\\VoicePill\\logs\\app.log`。**同一时刻只允许一个实例**：
两个实例都会采到同一支麦克风、都会粘贴，一次说话会被粘两遍（见
single_instance.py）。
"""
from __future__ import annotations

import argparse
import collections
import enum
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Callable, Optional

# 允许 `python src/main.py` 与 `python -m src.main` 两种跑法
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import console

console.make_output_safe()      # 必须在任何 print 之前；失败原因见 console.py

import autostart
import audio as audio_mod
import bridge
import config
import hotkey
import paste as paste_mod
import providers
import single_instance
from hotkey import HotkeyManager
from hud import HudWindow
from live_session import LiveTranscriptionSession


class Phase(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"


# 采到多少秒才算"这次录音有效"。
# 定 0.10 秒的依据：热键要求按住 ≥180 ms 才起录，而开流本身要吃掉约 176 ms
# （见 audio.py），所以最短的**有意图**的按住（约 0.3 秒）也能采到 ~0.12 秒，
# 卡在 0.10 不会误伤；而真正坏掉的情况实测就是 0.00 秒。低于这条线的音频
# 没有任何可转写的内容，送后端只会浪费一次往返、再拿回一句空话。
MIN_CAPTURE_SECONDS = 0.10

# 主循环的定期体检间隔（秒）。主循环本身 0.5 秒转一圈，见 VoicePill._tick。
HOOK_CHECK_SECONDS = 30.0        # 热键钩子还在不在
SETTINGS_CHECK_SECONDS = 2.0     # settings.json 有没有被改
MAINTENANCE_SECONDS = 6 * 3600.0  # 清一次过期留存

# 控制面 `take` 一次能取走的转写条数上限。取走语义 + 有界队列：用户说过的
# 话不会在内存里无限堆着，插件那侧也不会一次收到一大串。
PENDING_TRANSCRIPTS = 20


class RecordWatchdog:
    """单次录音的时长护栏：到点自己结束，不等松手事件。

    为什么必须有：整条链路里**唯一**的结束信号是主键的 keyup。这个信号会丢——
    实测出现过 32.27 秒的录音（用户早就松手了），也见过钩子被系统摘掉之后
    永远收不到松开。没有护栏的话麦克风会被一直占着，用户还以为是软件卡了。

    到点后的动作是「截断并照常转写」，不是「丢弃」：说出去的内容先说到的
    那部分已经采到了，扔掉等于白说。粘贴那一端还有前台窗口校验兜底
    （见 paste.py 第 4 条），窗口早换了就只留在剪贴板里，不会乱粘。

    `seconds <= 0` 表示不设上限。
    """

    def __init__(self, seconds: float, on_fire: Callable[[], None]) -> None:
        self.seconds = float(seconds)
        self._on_fire = on_fire
        self._timer: Optional[threading.Timer] = None
        self._fired = False

    def arm(self) -> None:
        self.disarm()
        self._fired = False
        if self.seconds <= 0:
            return
        self._timer = threading.Timer(self.seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def disarm(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    @property
    def fired(self) -> bool:
        return self._fired

    def _fire(self) -> None:
        self._fired = True
        self._on_fire()


class VoicePill:
    def __init__(self, settings: config.Settings, once: bool = False) -> None:
        self.settings = settings
        self.once = once
        self.phase = Phase.IDLE
        self._lock = threading.Lock()

        self.hud = HudWindow()
        self.capture = audio_mod.AudioCapture()
        self.session = LiveTranscriptionSession()

        self._target_hwnd = 0
        self._wav_path = ""
        self._log_path = ""
        self._transcript = ""
        self._last_error = ""
        # 最近几次成功的转写，供控制面 `take` 取走（Codex 插件那侧用）。
        # 每项是 (转写时间戳, 文字)：队列是被动的——它只在用户下一次往 Codex
        # 发消息时才被取走，所以得靠时间戳滤掉过了保鲜期的旧话（见
        # _drain_pending）。只在内存里，进程一退就没了——说过的话不该在
        # 磁盘上多留一份。
        self._pending = collections.deque(maxlen=PENDING_TRANSCRIPTS)
        self._started_at = time.time()
        # 退出信号。见 _shutdown 的注释：不能再用 sys.exit()。
        self._stop = threading.Event()
        self._stopping = False
        # 时长护栏（settings.max_record_seconds，0 = 不限）
        self._watchdog = RecordWatchdog(
            settings.max_record_seconds, self._on_max_duration)

        # 常驻形态的定期杂务（见 _tick / run）
        self._quit_event = 0
        self._quit_seen = False
        self._hook_restarts = 0
        self._settings_mtime = 0.0
        now = time.monotonic()
        self._next_hook_check = now + HOOK_CHECK_SECONDS
        self._next_settings_check = now + SETTINGS_CHECK_SECONDS
        # run() 里开机会立刻清一次，所以下一次排到 6 小时后
        self._next_maintenance = now + MAINTENANCE_SECONDS

        self.hotkeys = HotkeyManager(
            primary=settings.primary_spec,
            on_start=self.on_start,
            on_stop=self.on_stop,
            on_cancel=self.on_cancel,
            on_quick_tap=lambda: None,
        )

        # 控制面：外部进程（Codex 插件）靠它问状态、驱动录音、取走文字。
        # 见 bridge.py 顶部注释。
        self._bridge = bridge.BridgeServer(self._bridge_command)

    # ---------- 状态机 ----------

    def on_start(self) -> None:
        with self._lock:
            if self.phase is not Phase.IDLE:
                return
            self.phase = Phase.RECORDING

        self._last_error = ""
        self._transcript = ""
        # 记下要粘贴到哪个窗口（原版 VoicePill.swift:44 同）
        self._target_hwnd = paste_mod.foreground_window()

        provider = providers.get(self.settings.provider)
        self._wav_path = os.path.join(
            config.recordings_dir(),
            "rec-%s.wav" % datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        log_path = os.path.join(
            config.logs_dir(),
            "rec-%s.log" % datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        self._log_path = log_path

        self.session = LiveTranscriptionSession()
        self.session.start(
            binary_path=config.engine_path(provider.binary),
            provider=provider,
            log_path=log_path,
            punctuation=self.settings.doubao_punctuation,
            on_update=self.on_update,
            on_complete=self.on_complete,
        )

        try:
            # start() 立即返回，开设备在后台（open 实测 122~163 ms，比长按阈值
            # 还长，同步开会被松手时的 finish() 抢在前面，见 audio.py 注释）。
            # 所以设备的失败要经 on_error 回来，这里的 except 只兜同步段。
            self.capture.start(
                wav_path=self._wav_path,
                target_rate=provider.sample_rate,
                on_pcm=self.session.append,
                on_error=self._on_capture_error,
            )
        except Exception as exc:                      # 设备枚举失败等同步错误
            self.hud.show("麦克风打不开：%s" % exc)
            self.session.cancel()
            self._reset()
            return

        if self.settings.hud_enabled:
            self.hud.show("")
            self.hud.set_phase("listening")

        # 护栏放在最后起：前面的同步段抛异常时不会留下一个还在计时的定时器
        self._watchdog.arm()

    def _on_max_duration(self) -> None:
        """录音超时。在主循环之外的定时器线程上触发。"""
        with self._lock:
            if self.phase is not Phase.RECORDING:
                return
        limit = self.settings.max_record_seconds
        print("[护栏] 录音已到 %s 秒上限（松手事件大概丢了），按上限截断转写。"
              % limit, file=sys.stderr)
        if self.settings.hud_enabled:
            self.hud.set_text("已达最长录音时长（%s 秒），自动结束" % limit)
        self.on_stop()

    def _on_capture_error(self, exc: Exception) -> None:
        """开流失败（设备被独占、权限、拔了等）。在开流线程上回调。"""
        self._last_error = "麦克风打不开：%s" % exc
        if self.settings.hud_enabled:
            self.hud.show("麦克风打不开：%s" % exc)
        print("[麦克风] 开流失败：%r" % (exc,), file=sys.stderr)

    def on_update(self, text: str) -> None:
        """引擎的中间结果。**整句快照，直接覆盖**，不拼接。"""
        self._transcript = text
        if self.settings.hud_enabled:
            self.hud.set_text(text)

    def on_stop(self) -> None:
        with self._lock:
            if self.phase is not Phase.RECORDING:
                return
            self.phase = Phase.TRANSCRIBING

        self.capture.finish()          # 收尾 WAV（先落盘，供失败重试）

        # 采音体检。**必须在 session.finish() 之前**：空音频灌进去，引擎会
        # 如实回一句"收到 0 字节"，用户看到的是引擎的回显，以为是后端坏了——
        # 实际是麦克风一帧没出（设备被别的进程占着）。实测踩过一次：上一轮的
        # 僵尸进程握着麦克风，新流能开起来但收不到帧，链路看着全绿，结果为空。
        got = self.capture.seconds_captured
        if got < MIN_CAPTURE_SECONDS:
            self._abort_capture(got)
            return

        if self.settings.hud_enabled:
            self.hud.set_phase("transcribing")
        self.session.finish()          # 关 stdin，让引擎收尾出结果

    def _abort_capture(self, got: float) -> None:
        """录音无效：不送后端，直接收摊。"""
        if got <= 0.0:
            reason = "麦克风没出声（0 秒）"
            hint = "设备可能被别的程序占着（含上一次没退干净的 Voice Pill），或刚被拔插。"
        else:
            reason = "录音太短（%.2f 秒）" % got
            hint = "按住主键的时间再长一点。"
        self._last_error = reason
        print("[跳过] %s —— %s" % (reason, hint), file=sys.stderr)
        self.session.cancel()          # 引擎都没必要启动，直接撤
        self._remove_wav()             # 空录音没有留存价值
        self._remove_log()
        if self.settings.hud_enabled:
            self.hud.set_text("%s" % reason)
            threading.Timer(2.5, self.hud.hide).start()
        self._reset()
        if self.once:
            self._shutdown()

    def on_cancel(self) -> None:
        with self._lock:
            if self.phase is not Phase.RECORDING:
                return
        self.capture.cancel()
        self.session.cancel()
        if self.settings.hud_enabled:
            self.hud.hide()
        self._reset()

    def on_complete(self, text, error) -> None:
        """会话结束。text 与 error 恰有一个非空。"""
        if error:
            self._last_error = error
            if self.settings.hud_enabled:
                self.hud.set_text("失败：%s" % error)
                threading.Timer(2.5, self.hud.hide).start()
            # 原版语义：失败时保留录音，供 Retry。这里不删 WAV。
            print("[失败] %s\n       录音留存：%s" % (error, self._wav_path),
                  file=sys.stderr)
            self._reset()
            if self.once:
                self._shutdown()
            return

        self._transcript = text or ""
        if self._transcript:
            with self._lock:
                self._pending.append((time.time(), self._transcript))
        if self.settings.auto_paste and self._transcript:
            try:
                paste_mod.paste(self._transcript, self._target_hwnd)
            except paste_mod.PasteError as exc:
                # 粘贴失败**不还原剪贴板**——文字还在，用户可手动 Ctrl+V
                print("[粘贴失败] %s\n       文字已在剪贴板：%s"
                      % (exc, self._transcript), file=sys.stderr)
        else:
            print(self._transcript)

        # 转写成功 → 删掉录音（原版同）
        self._remove_wav()
        self._remove_log()
        if self.settings.hud_enabled:
            self.hud.hide()
        self._reset()
        if self.once:
            self._shutdown()

    # ---------- 杂项 ----------

    def _bridge_command(self, cmd: str, args: dict):
        """控制面回调。跑在该连接自己的线程上，不是在主循环里。

        只做转发，不在这层加判断：状态机的准入条件已经在 on_start /
        on_stop / on_cancel 里了（比如"已经在录了"就直接忽略）。这里再判一遍
        只会多出一份会跟那边走散的规则。
        """
        if cmd == "status":
            return self._bridge_status()
        if cmd == "take":
            texts = self._drain_pending()
            return {"texts": texts, "count": len(texts)}
        if cmd == "start":
            self.on_start()
        elif cmd == "stop":
            self.on_stop()
        elif cmd == "cancel":
            self.on_cancel()
        return self._bridge_status()

    def _pending_ttl_seconds(self) -> float:
        """待取文字的保鲜期（秒）。settings 里 0 = 不过期。"""
        return max(0.0, float(self.settings.pending_ttl_minutes)) * 60.0

    def _drain_pending(self) -> list:
        """取走队列里所有文字（旧→新），顺带丢掉过了保鲜期的。

        为什么要有保鲜期：队列里的字只在用户下一次往 Codex 发消息时才被
        取走。没有时效的话，几天前随口说的一句会在几天后被一起塞进一轮
        对话，和当时真正的意图搅在一起。取走语义照旧——过期的那些既不
        返回、也不再留在队列里。
        """
        ttl = self._pending_ttl_seconds()
        cutoff = time.time() - ttl if ttl > 0 else None
        # 一起持锁：list 和 clear 之间若插进一次 append，那条会被漏掉。
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
        return [text for stamp, text in items
                if cutoff is None or stamp >= cutoff]

    def _pending_count(self) -> int:
        """还没过保鲜期的条数（`status` 用）。只报数，不动队列。"""
        ttl = self._pending_ttl_seconds()
        if ttl <= 0:
            return len(self._pending)
        cutoff = time.time() - ttl
        with self._lock:
            return sum(1 for stamp, _ in self._pending if stamp >= cutoff)

    def _bridge_status(self) -> dict:
        provider = providers.get(self.settings.provider)
        return {
            "pid": os.getpid(),
            "phase": self.phase.value,
            "provider": provider.key,
            "provider_label": provider.label,
            "hotkey": self.settings.primary_key,
            "hotkey_alive": bool(self.hotkeys.alive),
            "hud": self.settings.hud_enabled,
            "pending": self._pending_count(),
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }

    def _remove_wav(self) -> None:
        if self._wav_path and os.path.isfile(self._wav_path):
            try:
                os.remove(self._wav_path)
            except OSError:
                pass

    def _remove_log(self) -> None:
        """转写成功后删掉本次的实时日志。

        这些 `rec-*.log` 是引擎 stderr 的留存，只有**失败**时才有人看
        （live_session 的 _read_log_tail 就是读它）。成功还留着，就是每次
        录音留一个文件、永不清理——实测这类文件在真实使用里攒得飞快。
        失败的走另一条路：保留，由 config.prune_data 按天清。
        """
        if self._log_path and os.path.isfile(self._log_path):
            try:
                os.remove(self._log_path)
            except OSError:
                pass

    def _reset(self) -> None:
        with self._lock:
            self.phase = Phase.IDLE
        self._watchdog.disarm()
        self.hotkeys.reset()

    def run(self) -> int:
        if self.settings.hud_enabled:
            self.hud.start()
        self.hotkeys.start()
        self._bridge.start()
        self._quit_event = single_instance.open_quit_event()

        print("Voice Pill 已启动。")
        print("  后端    ：%s" % providers.get(self.settings.provider).label)
        print("  主键    ：%s —— 按住说话，松开粘贴（轻点不触发）"
              % hotkey.describe_primary(self.settings.primary_spec))
        print("  备用键  ：Ctrl + Alt + Space（按一次开始，再按一次结束）")
        print("  取消    ：录音中按 Esc")
        print("  最长录音：%s" % ("%s 秒" % self.settings.max_record_seconds
                                 if self.settings.max_record_seconds > 0
                                 else "不限"))
        print("  Ctrl+C 退出；驻留形态用 `main.py --stop`")
        print("  控制面  ：%s" % bridge.PIPE_NAME)

        self._settings_mtime = _mtime(config.Settings.path())
        self._run_maintenance()          # 开机先清一次过期留存

        try:
            # 主循环只负责「等退出信号」和定期杂务。
            while not self._stop.wait(0.5):
                if single_instance.quit_requested(self._quit_event):
                    if not self._waiting_to_quit():
                        break
                # 定期杂务出错绝不能打死驻留进程：settings.json 里一个类型写错的
                # 字段（比如把 120 写成 "120秒"）会被热重载带进来，如果它顺手
                # 崩掉主循环，进程就退出、看门狗再拉、再崩——直接进连崩放弃。
                # 宁可这一轮杂务跳过，也要把服务留住。
                try:
                    self._tick()
                except Exception as exc:              # noqa: BLE001
                    print("[杂务] %r" % (exc,), file=sys.stderr)
        except KeyboardInterrupt:
            pass
        self._shutdown()
        # tkinter 只能在主线程收摊，所以 hud.quit() 留在这，不在 _shutdown 里
        self.hud.quit()
        return 0

    def _waiting_to_quit(self) -> bool:
        """收到 `--stop`：能不打断录音就别打断。返回 True 表示还要继续等。"""
        if self.phase is Phase.IDLE:
            print("收到 --stop，退出。")
            return False
        if not self._quit_seen:
            self._quit_seen = True
            print("收到 --stop，等这次录音收尾再退。")
        return True

    # ---------- 定期杂务（都在主线程）----------

    def _tick(self) -> None:
        now = time.monotonic()
        if now >= self._next_hook_check:
            self._next_hook_check = now + HOOK_CHECK_SECONDS
            self._check_hook()
        if now >= self._next_settings_check:
            self._next_settings_check = now + SETTINGS_CHECK_SECONDS
            self._check_settings()
        if now >= self._next_maintenance:
            self._next_maintenance = now + MAINTENANCE_SECONDS
            self._run_maintenance()

    def _check_hook(self) -> None:
        """钩子死了就重装。驻留形态下"按 Fn 没反应"是最难自查的故障。"""
        if self.hotkeys.alive:
            return
        print("[热键] 钩子线程不在了，重装。", file=sys.stderr)
        try:
            self.hotkeys.restart()
        except Exception as exc:                      # 重装本身炸了也不该拖死主循环
            print("[热键] 重装失败：%r" % (exc,), file=sys.stderr)
            return
        self._hook_restarts += 1
        if self.hotkeys.hook_installed:
            print("[热键] 已重装（累计 %d 次）。" % self._hook_restarts,
                  file=sys.stderr)
        else:
            print("[热键] 重装后仍装不上，err=%d。热键当前不可用。"
                  % self.hotkeys.hook_error, file=sys.stderr)

    def _check_settings(self) -> None:
        """settings.json 改了就地生效，省得为了换个后端去重启驻留进程。"""
        path = config.Settings.path()
        mtime = _mtime(path)
        if not mtime or mtime == self._settings_mtime:
            return
        self._settings_mtime = mtime
        try:
            new = config.Settings.load()
        except Exception as exc:
            print("[配置] 读不出来，忽略这次改动：%r" % (exc,), file=sys.stderr)
            return
        self._apply_settings(new)

    def _apply_settings(self, new: config.Settings) -> None:
        old = self.settings
        changes = []

        if new.provider != old.provider:
            changes.append("后端 %s → %s" % (old.provider, new.provider))
        if new.primary_key != old.primary_key:
            applied = self.hotkeys.set_primary(new.primary_spec)
            changes.append("主键 %s → %s%s" % (
                old.primary_key, new.primary_key,
                "" if applied else "（正在录音，本次结束后生效）"))
        if new.auto_paste != old.auto_paste:
            changes.append("自动粘贴 → %s" % new.auto_paste)
        if new.doubao_punctuation != old.doubao_punctuation:
            changes.append("豆包标点 → %s" % new.doubao_punctuation)
        if new.max_record_seconds != old.max_record_seconds:
            changes.append("最长录音 → %s 秒" % new.max_record_seconds)
        if new.retention_days != old.retention_days:
            changes.append("留存天数 → %s" % new.retention_days)
        if new.local_model_dir != old.local_model_dir:
            changes.append("本地模型目录 → %s" % new.local_model_dir)
        if new.hud_enabled != old.hud_enabled:
            # 悬浮条的 tk 线程起停不方便中途切换，如实说明，不假装生效
            changes.append("悬浮条 → %s（需重启生效）" % new.hud_enabled)

        self.settings = new
        # 护栏的下一次武装用新值；已经在录的那次不改变时长
        self._watchdog.seconds = float(new.max_record_seconds)
        if changes:
            print("[配置] 已重载：%s" % "；".join(changes))
        else:
            print("[配置] settings.json 变了，但可热改的项没有变化。")

    def _run_maintenance(self) -> None:
        try:
            gone = config.prune_data(self.settings.retention_days)
        except Exception as exc:
            print("[清理] 出错：%r" % (exc,), file=sys.stderr)
            return
        if gone:
            print("[清理] 删掉 %d 个过期文件（保留 %s 天）。"
                  % (len(gone), self.settings.retention_days))

    def _shutdown(self) -> None:
        """要求进程退出。**可从任意线程调用**。

        这里曾经是 `sys.exit(0)`，是个隐蔽的坑：`on_complete` 由
        `live_session._read_loop` 的**读线程**经 `threading.Thread` 回调进来，
        而 `sys.exit()` 在非主线程只抛给本线程，**主线程的循环照转不误**——
        结果是 `--once` 跑完不出结果也永不退出，麦克风一直被占着（实测挂了
        三分钟以上）。改成置事件、由主线程走完整退出流程。

        tkinter 的收摊必须在主线程做，所以 hud.quit() 留在 run() 里。
        """
        if self._stopping:
            return
        self._stopping = True
        self._stop.set()
        self._bridge.stop()
        self.hotkeys.stop()
        single_instance.close_handle(self._quit_event)
        self._quit_event = 0


# ---------- 进程级入口的小工具 ----------

def _mtime(path: str) -> float:
    """文件修改时间；不在就返回 0（= 用默认值，不是错误）。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _journal_exit(code: int) -> None:
    """在日志里留一条收尾行。

    驻留形态下进程没有窗口，"崩了"和"活着但钩子哑了"从外面看一模一样。
    有这条和启动横幅配对的收尾行，翻 app.log 就能分清是正常停的还是被打死的：
    启动横幅有、收尾行没有 = 非正常死亡。
    """
    print("\n=== 退出 %s code=%d ==="
          % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code))


def stop_running_instance() -> int:
    """`--stop`：让正在驻留的那个实例走正常退出流程。"""
    probe = single_instance.InstanceLock()
    try:
        if probe.acquire():
            probe.release()
            print("没有正在运行的 Voice Pill 实例。")
            return 1
    except OSError as exc:
        print("读不到实例状态：%s" % exc, file=sys.stderr)
        return 1
    if not single_instance.request_quit():
        print("退出信号发不出去。", file=sys.stderr)
        return 1
    print("已请求退出（若它正在录音，会等这次收尾）。日志：%s"
          % config.app_log_path())
    return 0


# ---------- 自检 ----------

def run_check(settings: config.Settings) -> int:
    ok = True
    print("=" * 60)
    print("Voice Pill Windows 自检")
    print("=" * 60)

    print("\n[引擎]")
    for key in ("local", "doubao", "codex"):
        p = providers.get(key)
        path = config.engine_path(p.binary)
        exists = os.path.isfile(path)
        # 用解析后的文件名而不是 p.binary + ".exe"：本地引擎是 .py，加 .exe 会串味
        print("  %-8s %-24s %s" % (key, os.path.basename(path),
                                   "✅ " + path if exists else "❌ 缺失 " + path))
        if not exists and key == settings.provider:
            ok = False

    print("\n[本地模型]")
    model_dir = config.local_model_dir()
    print("  目录      ：%s" % model_dir)
    for name in ("model.int8.onnx", "tokens.txt"):
        exists = os.path.isfile(os.path.join(model_dir, name))
        print("  %-16s %s" % (name, "✅" if exists else "❌ 缺失"))
        if not exists and settings.provider == "local":
            ok = False
    try:
        import sherpa_onnx                       # noqa: F401
        print("  %-16s ✅ 已安装" % "sherpa-onnx")
    except ImportError:
        print("  %-16s ❌ 未安装（pip install sherpa-onnx）" % "sherpa-onnx")
        if settings.provider == "local":
            ok = False

    print("\n[麦克风]")
    try:
        rate = audio_mod.AudioCapture.default_input_rate()
        devs = audio_mod.AudioCapture.list_input_devices()
        print("  默认采样率：%d Hz" % rate)
        print("  输入设备：%d 个" % len(devs))
        for idx, name in devs[:5]:
            print("    [%d] %s" % (idx, name))
    except Exception as exc:
        print("  ❌ 无法枚举音频设备：%s" % exc)
        ok = False

    print("\n[配置]")
    print("  目录      ：%s" % config.app_dir())
    print("  后端      ：%s" % settings.provider)
    print("  主键      ：%s → %s" % (settings.primary_key,
                                     hotkey.describe_primary(settings.primary_spec)))
    print("  自动粘贴  ：%s" % settings.auto_paste)
    print("  最长录音  ：%s 秒（0 = 不限）" % settings.max_record_seconds)
    print("  留存天数  ：%s（0 = 不清理）" % settings.retention_days)

    print("\n[常驻]")
    probe = single_instance.InstanceLock()
    running = not probe.acquire()
    if not running:
        probe.release()
    print("  实例      ：%s" % ("✅ 已有实例在跑" if running else "无（未驻留）"))
    guard = single_instance.InstanceLock(single_instance.SUPERVISOR_MUTEX_NAME)
    watching = not guard.acquire()
    if not watching:
        guard.release()
    print("  看门狗    ：%s" % ("✅ 在守着" if watching else "无（没人替你重拉）"))
    print("  控制面    ：%s" % bridge.status_line())
    print("  开机自启  ：%s" % autostart.status())
    log = config.app_log_path()
    size = os.path.getsize(log) if os.path.isfile(log) else 0
    print("  应用日志  ：%s（%d KB）" % (log, size // 1024))

    print("\n[豆包凭据]")
    cred = config.credentials_path()
    has_cred = os.path.isfile(cred)
    print("  %s %s" % ("✅" if has_cred else "⚠️ ", cred))
    if not has_cred:
        print("      → 跑 bin\\freeasr.exe auth --credential-path \"%s\" 生成" % cred)
        if settings.provider == "doubao":
            ok = False

    print("\n[Codex 凭据]")
    auth = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
    if not os.path.isfile(auth):
        print("  ❌ 缺失 %s" % auth)
        if settings.provider == "codex":
            ok = False
    else:
        # 光看文件在不在会骗人：本机 auth.json 只有 39 字节、一个 13 字符的
        # OPENAI_API_KEY 占位符，没有 tokens —— 引擎照样报
        # "does not contain a ChatGPT access token"。所以要看内容。
        has_oauth = False
        note = ""
        try:
            with open(auth, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            toks = data.get("tokens") or {}
            has_oauth = bool(toks.get("access_token"))
            if not has_oauth:
                key = data.get("OPENAI_API_KEY") or ""
                note = "只有 OPENAI_API_KEY（%d 字符），无 ChatGPT OAuth 令牌" % len(key)
        except (OSError, ValueError) as exc:
            note = "读不出来：%s" % exc

        if has_oauth:
            print("  ✅ %s（含 ChatGPT OAuth 令牌）" % auth)
        else:
            print("  ⚠️  %s" % auth)
            print("      %s" % note)
            print("      → Codex 后端要用 ChatGPT 登录后的令牌，API Key 不行。")
            print("        跑 `codex login` 重新登录。")
            if settings.provider == "codex":
                ok = False

    print("\n" + "=" * 60)
    print("结论：%s" % ("✅ 就绪" if ok else "❌ 有缺失项，见上"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Voice Pill for Windows")
    ap.add_argument("--check", action="store_true", help="自检")
    ap.add_argument("--list-devices", action="store_true", help="列麦克风")
    ap.add_argument("--retry", action="store_true", help="重试留存录音")
    ap.add_argument("--once", action="store_true", help="录一次就退出")
    ap.add_argument("--stop", action="store_true", help="让正在运行的实例退出")
    ap.add_argument("--install-autostart", action="store_true", help="装开机自启")
    ap.add_argument("--uninstall-autostart", action="store_true", help="取消开机自启")
    args = ap.parse_args()

    # 先把日志接上：驻留时（`pythonw.exe` 开机自启）没有控制台，后面所有 print
    # 都会是静默空操作，崩了也没人知道为什么。有控制台时会再镜像一份到屏幕。
    console.attach_app_log(config.app_log_path(),
                           label=" ".join(sys.argv[1:]) or "驻留启动")

    settings = config.Settings.load()

    if args.list_devices:
        for idx, name in audio_mod.AudioCapture.list_input_devices():
            print("[%d] %s" % (idx, name))
        return 0

    if args.check:
        return run_check(settings)

    if args.stop:
        return stop_running_instance()

    if args.install_autostart:
        print("即将安装开机自启：\n  %s" % autostart.describe())
        return autostart.install()

    if args.uninstall_autostart:
        return autostart.uninstall()

    if args.retry:
        print("Retry 尚未实现。留存录音在：%s" % config.recordings_dir())
        return 1

    # 从这里往下都会真的录音，必须独占：两个实例会各采一遍同一支麦克风、
    # 各粘一遍，用户看到的是同一句话出现两次。
    lock = single_instance.InstanceLock()
    try:
        if not lock.acquire():
            print("已经有一个 Voice Pill 实例在跑，本进程退出。\n"
                  "  两个实例会同时采音、同时粘贴，一次说话会被粘两遍。\n"
                  "  要停掉那个：python main.py --stop", file=sys.stderr)
            return 3
    except OSError as exc:
        print("拿不到单实例锁：%s" % exc, file=sys.stderr)
        return 1

    code = 0
    try:
        code = VoicePill(settings, once=args.once).run()
    except Exception:
        # 非零退出是给计划任务的"失败后重启"看的，别吞掉
        traceback.print_exc()
        code = 1
    finally:
        lock.release()
    _journal_exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
