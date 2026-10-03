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
    python main.py --retry          重试留存的失败录音
    python main.py --once           录一次就退出（调试用）
"""
from __future__ import annotations

import argparse
import enum
import json
import os
import sys
import threading
import time
from datetime import datetime

# 允许 `python src/main.py` 与 `python -m src.main` 两种跑法
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import console

console.make_output_safe()      # 必须在任何 print 之前；失败原因见 console.py

import audio as audio_mod
import config
import hotkey
import paste as paste_mod
import providers
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
        self._transcript = ""
        self._last_error = ""
        # 退出信号。见 _shutdown 的注释：不能再用 sys.exit()。
        self._stop = threading.Event()
        self._stopping = False

        self.hotkeys = HotkeyManager(
            primary=settings.primary_spec,
            on_start=self.on_start,
            on_stop=self.on_stop,
            on_cancel=self.on_cancel,
            on_quick_tap=lambda: None,
        )

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
        if self.settings.hud_enabled:
            self.hud.hide()
        self._reset()
        if self.once:
            self._shutdown()

    # ---------- 杂项 ----------

    def _remove_wav(self) -> None:
        if self._wav_path and os.path.isfile(self._wav_path):
            try:
                os.remove(self._wav_path)
            except OSError:
                pass

    def _reset(self) -> None:
        with self._lock:
            self.phase = Phase.IDLE
        self.hotkeys.reset()

    def run(self) -> None:
        if self.settings.hud_enabled:
            self.hud.start()
        self.hotkeys.start()
        print("Voice Pill 已启动。")
        print("  后端    ：%s" % providers.get(self.settings.provider).label)
        print("  主键    ：%s —— 按住说话，松开粘贴（轻点不触发）"
              % hotkey.describe_primary(self.settings.primary_spec))
        print("  备用键  ：Ctrl + Alt + Space（按一次开始，再按一次结束）")
        print("  取消    ：录音中按 Esc")
        print("  Ctrl+C 退出")
        try:
            # 主循环只负责「等退出信号」。
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        self._shutdown()
        # tkinter 只能在主线程收摊，所以 hud.quit() 留在这，不在 _shutdown 里
        self.hud.quit()

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
        self.hotkeys.stop()


# ---------- 自检 ----------

def run_check(settings: config.Settings) -> int:
    ok = True
    print("=" * 60)
    print("Voice Pill Windows 自检")
    print("=" * 60)

    print("\n[引擎]")
    for key in ("codex", "doubao"):
        p = providers.get(key)
        path = config.engine_path(p.binary)
        exists = os.path.isfile(path)
        print("  %-8s %-14s %s" % (key, p.binary + ".exe",
                                   "✅ " + path if exists else "❌ 缺失 " + path))
        if not exists and key == settings.provider:
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
    args = ap.parse_args()

    settings = config.Settings.load()

    if args.list_devices:
        for idx, name in audio_mod.AudioCapture.list_input_devices():
            print("[%d] %s" % (idx, name))
        return 0

    if args.check:
        return run_check(settings)

    if args.retry:
        print("Retry 尚未实现。留存录音在：%s" % config.recordings_dir())
        return 1

    app = VoicePill(settings, once=args.once)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
