# -*- coding: utf-8 -*-
"""发声（离线 TTS）自测：模型、文本清理、切句、合成、打断、静置释放。

为什么需要它
------------
发声这条路"看起来能出声"不等于"能对话"。真正会坏的是三处，且都不出声时
看不出来：
  * 模型目录解析错了 —— 换台机器/挪个目录就哑了
  * **打断不管用** —— 你说到一半想插话，它还在念，那就成了广播
  * **静置不释放** —— 常驻进程背着几百 MB 不放，几天后内存越来越难看

默认**不发声**（`_open_stream` 被打桩成一条假流），免得跑一次自测就在屋里念起来。
要真听一次：加 `--play`，它只念一句。

用法：
    .venv\\Scripts\\python.exe tools\\speak-selftest.py [--play]
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

import config                      # noqa: E402
import speak as speak_mod          # noqa: E402


# 句间空档的上限（秒）。原先写 0.15——那是照「逐句重开流 0.34 秒/句 + 串行合成」
# 那个病定的，量的是理想值。2026-10-03 复测发现：这个量**由负载主导**，同一份代码
# 在 0.15 ~ 1.00 秒之间漂（预取位 1 → 2 试过，0.57~1.02 vs 0.53~1.00，没有改善，
# 已回退）。所以阈值不能再按理想值卡，改成「没有大段停滞」：当年那个串行实现是
# 每句 2 秒以上的空档，1.5 秒这条线正好只抓灾难性回归。真实趋势看每次打印的实测值。
MAX_BOUNDARY_GAP_SECONDS = 1.5


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        print("  %s %s%s" % ("✅" if ok else "❌", label,
                             "" if ok else ("  ← %s" % detail if detail else "")))
        if not ok:
            self.failed += 1
        return ok


def wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def title(fn) -> str:
    doc = (fn.__doc__ or fn.__name__).strip()
    return doc.splitlines()[0]


# 两个**实测**出来的代价（见 docs/移植方案.md 16.4）。假流必须照着它来，
# 否则"连贯性"这条根本测不出来：一个不收开流费、也不按实时阻塞的假流，
# 连"逐句重开流"那种写法都能跑得飞快，等于没测。
STREAM_OPEN_COST = 0.34        # sd.OutputStream 开一次关一次（首次 0.95）
WRITE_SECONDS_PER_SECOND = 1.03  # write() 按实时走：写 1 秒音频阻塞 1.03 秒


class FakeStream:
    """给 Speaker._open_stream 打桩：不出声，但代价照真实来。"""

    def __init__(self, rate: int, sink: "Sink") -> None:
        self.rate = rate
        self._sink = sink
        self.aborted = False
        self.closed = False

    def start(self) -> None:
        time.sleep(STREAM_OPEN_COST)
        self._sink.opened += 1

    def write(self, samples) -> None:
        dur = len(samples) / self.rate
        begin = time.monotonic()
        self._sink.spans.append((begin, begin + dur, dur))
        end = begin + dur * WRITE_SECONDS_PER_SECOND
        while time.monotonic() < end:
            if self.aborted:
                return
            time.sleep(min(0.03, max(0.0, end - time.monotonic())))

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True


class Sink:
    """收集假流上发生的事：开了几条流、每条写了多久、句间空了多少。"""

    def __init__(self) -> None:
        self.opened = 0
        self.spans = []
        self._lock = threading.Lock()

    def __call__(self, rate: int) -> "FakeStream":
        return FakeStream(rate, self)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.spans)

    @property
    def total(self) -> float:
        with self._lock:
            return sum(d for _, _, d in self.spans)

    @property
    def streams_opened(self) -> int:
        return self.opened

    def first_write_at(self) -> float:
        with self._lock:
            return min([s[0] for s in self.spans], default=0.0)

    def max_boundary_gap(self) -> float:
        """相邻两次写入之间的空档（秒）。这就是"流水线自己加的死气"。"""
        with self._lock:
            spans = sorted(self.spans)
        gaps = [spans[i + 1][0] - spans[i][1] for i in range(len(spans) - 1)]
        return max(gaps) if gaps else 0.0


def check_model(ck: Checker) -> None:
    """模型解析到 D 盘那份，且文件齐全。"""
    root = config.tts_model_dir()
    ck("TTS 模型目录能解析出来", bool(root), root)
    for name in ("model.onnx", "voices.bin", "tokens.txt", "espeak-ng-data",
                 "lexicon-zh.txt", "lexicon-us-en.txt"):
        ck("模型文件 %s" % name, os.path.exists(os.path.join(root, name)), root)
    ck("模型与 ASR 模型并排（不写死盘符的推导）",
       os.path.dirname(root) == os.path.dirname(config.local_model_dir()),
       "%s vs %s" % (root, config.local_model_dir()))


def check_text_cleanup(ck: Checker) -> None:
    """不该念的东西不念，该念的内容一个字不改。"""
    raw = ("改完了。\n\n```python\nprint(1)\n```\n"
           "详见 [文档](https://example.com/x) 和 `src/main.py`。\n"
           "- **要点**一\n")
    out = speak_mod.strip_markdown(raw)
    ck("代码块变成提示语，不再逐字念代码", "print(1)" not in out)
    ck("链接只留标题、不留地址", "https://example.com" not in out and "文档" in out)
    ck("行内代码去掉反引号", "`" not in out and "src/main.py" in out)
    ck("列表/标题记号被去掉", not out.lstrip().startswith(("-", "#")))
    ck("正文内容没被改写", "改完了" in out and "要点一" in out)


def check_sentence_split(ck: Checker) -> None:
    """切句：中英标点都要认，超长句要断。"""
    parts = speak_mod.split_sentences("第一句。第二句！第三句？后面还有")
    ck("按中英标点切句", len(parts) == 4, "实际 %r" % (parts,))
    long_one = "很长的一段话" * 20
    ck("超长句会被断开",
       all(len(p) <= 130 for p in speak_mod.split_sentences(long_one)),
       "实际 %r" % ([len(p) for p in speak_mod.split_sentences(long_one)],))


def check_synthesis(ck: Checker, rec: Sink, settings) -> None:
    """真合成一句：不发声，但音频必须真的出来。"""
    sp = speak_mod.Speaker(lambda: settings)
    sp._open_stream = rec                           # 打桩：不出声
    t0 = time.time()
    spoken = sp.speak("好了，改完了。三处都过了测试。")
    ck("speak() 立刻返回、不阻塞调用方", time.time() - t0 < 0.5,
       "耗时 %.2fs" % (time.time() - t0))
    ck("返回实际会念的文本", spoken.startswith("好了"))
    ck("合成并播完", wait_until(lambda: not sp.speaking, 120) and rec.count >= 1,
       "已播 %d 段" % rec.count)
    ck("音频时长合理（0.5~15 秒）", 0.5 <= rec.total <= 15,
       "实际 %.2fs" % rec.total)
    t0 = time.time()
    sp._ensure_model()
    ck("第二次调用不再重复加载模型（懒加载只发生一次）",
       time.time() - t0 < 0.1, "耗时 %.2fs" % (time.time() - t0))


def check_interrupt(ck: Checker, settings) -> None:
    """打断必须立刻生效——这是「对话」与「广播」的分界。"""
    rec = Sink()
    sp = speak_mod.Speaker(lambda: settings)
    sp._open_stream = rec
    long_text = "。".join("第%d句，这是一句要念一会儿的话" % i for i in range(1, 13))
    sp.speak(long_text)
    time.sleep(0.6)
    ck("长时间说话时状态为「在说」", sp.speaking or rec.count >= 1)
    t0 = time.time()
    sp.stop()
    ck("stop() 后说话状态很快收敛", wait_until(lambda: not sp.speaking, 3.0),
       "耗时 %.2fs" % (time.time() - t0))
    played_at_stop = rec.count
    time.sleep(1.0)
    ck("stop() 之后不再有新句子播出去", rec.count == played_at_stop,
       "%d → %d" % (played_at_stop, rec.count))
    ck("stop() 清空了待播队列", list(sp._queue) == [], "队列 %r" % (list(sp._queue),))


def check_continuity(ck: Checker, settings) -> None:
    """句与句之间不许有流水线自己加的死气——这是"连贯性"的可测形式。

    用户报的原话是某条音色"连贯性表达效果差很多"。量下来根因在流水线，不在
    音色：逐句重开输出流（0.34 秒/句）＋串行合成（下一句的合成耗时整段落在
    停顿里）。所以这里量的是**行为**：整段只许开一条流，且相邻两次写入之间
    不许有空档。
    """
    rec = Sink()
    sp = speak_mod.Speaker(lambda: settings)
    sp._open_stream = rec
    text = "好了，都改完了。三个文件全过了测试，要不要我现在 commit？"
    n = len(speak_mod.split_sentences(text))
    ck("这段话确实会被切成多句（切成一句这条就测不出东西）", n >= 2,
       "切了 %d 句" % n)

    sp._ensure_model()          # 先热身：冷启动那 ~2 秒加载不算在连贯性里
    t0 = time.time()
    sp.speak(text)
    ck("念完了", wait_until(lambda: not sp.speaking, 180))
    wall = time.time() - t0

    ck("整段只开一条输出流（不再逐句重开）", rec.streams_opened == 1,
       "开了 %d 条" % rec.streams_opened)
    print("  · 实测最大空档 %.2fs（上限 %.2f 秒）" % (rec.max_boundary_gap(), MAX_BOUNDARY_GAP_SECONDS))
    ck("句间没有死气（<= %.2f 秒）" % MAX_BOUNDARY_GAP_SECONDS,
       rec.max_boundary_gap() <= MAX_BOUNDARY_GAP_SECONDS,
       "最大空档 %.2fs" % rec.max_boundary_gap())
    ck("开头不用等太久（第一声 <= 2.5 秒）", rec.first_write_at() - t0 <= 2.5,
       "%.2fs" % (rec.first_write_at() - t0))
    ck("总耗时接近音频本身（没有把合成串在播放中间）",
       wall <= rec.total + STREAM_OPEN_COST + 2.0,
       "总 %.2fs，音频 %.2fs" % (wall, rec.total))


def check_stop_hook_is_safe(ck: Checker) -> None:
    """Stop 钩子绝不能拖慢或拦住回合：任何输入都要吐 continue，且要够快。"""
    import json as _json
    import subprocess
    import tempfile

    hook = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plugins", "voice-pill", "hook_stop.py")
    ck("钩子脚本存在", os.path.isfile(hook), hook)

    cases = [
        ('{"hook_event_name":"Stop","session_id":"s","last_assistant_message":"改完了。"}', "有正文"),
        ('{}', "空载荷"),
        ('{"last_assistant_message":""}', "空正文"),
        ("not json", "不是 JSON"),
    ]
    # 两个环境变量缺一不可，都是被真身踩过之后加的：
    #   * LOCALAPPDATA 指到临时目录 → 造出另一把密钥（模拟"密钥文件被删掉重建"）
    #   * VOICEPILL_PIPE_NAME 换成测试专用名 → **不碰真身的控制面**
    # 只用前者是不够的：管道名以前是全局的，钩子会连上真身、握手失败，然后把
    # 真身的 accept 线程打死（控制面永久失联），而测试自己还慢到超时。
    dead = tempfile.mkdtemp(prefix="voicepill-hook-")
    env = dict(os.environ, LOCALAPPDATA=dead,
               VOICEPILL_PIPE_NAME=r"\\.\pipe\VoicePill-selftest-hook-%d" % os.getpid(),
               PYTHONIOENCODING="utf-8")
    for payload, label in cases:
        t0 = time.time()
        proc = subprocess.run([sys.executable, hook], input=payload,
                              capture_output=True, text=True, timeout=30,
                              env=env, encoding="utf-8")
        dt = time.time() - t0
        out = (proc.stdout or "").strip()
        ck("「%s」退出码 0（不把回合搞崩）" % label, proc.returncode == 0,
           "rc=%s err=%s" % (proc.returncode, (proc.stderr or "")[-120:]))
        ck("「%s」输出一行合法 JSON 且 continue" % label,
           bool(out) and _json.loads(out).get("continue") is True, "out=%r" % out)
        ck("「%s」够快（< 5 秒）" % label, dt < 5.0, "耗时 %.2fs" % dt)


def check_idle_unload(ck: Checker, settings) -> None:
    """静置够久要把模型放掉，常驻进程不能一直背着它。"""
    rec = Sink()
    sp = speak_mod.Speaker(lambda: settings)
    sp._open_stream = rec
    sp.speak("这一句用来把模型加载起来。")
    wait_until(lambda: not sp.speaking, 120)
    ck("模型已加载", sp.model_loaded)
    settings.tts_idle_unload_seconds = 1000
    ck("没到时间不释放", not sp.maybe_unload())
    settings.tts_idle_unload_seconds = 0
    ck("0 = 一直留着（不释放）", not sp.maybe_unload())
    settings.tts_idle_unload_seconds = 1000
    sp._last_use = time.monotonic() - 2000
    ck("静置超时就释放", sp.maybe_unload() and not sp.model_loaded)
    ck("释放后还能再加载回来（不会一次释放就哑掉）",
       wait_until(lambda: sp._ensure_model() is not None, 120))


def main() -> int:
    do_play = "--play" in sys.argv
    settings = config.Settings()
    settings.hud_enabled = False
    ck = Checker()
    print("=== 发声（离线 TTS）自测 ===")
    print("模型：%s" % config.tts_model_dir())

    for fn in (check_model, check_text_cleanup, check_sentence_split):
        print("\n[%s]" % title(fn))
        fn(ck)

    rec = Sink()
    print("\n[%s]" % title(check_synthesis))
    check_synthesis(ck, rec, settings)

    print("\n[%s]" % title(check_continuity))
    check_continuity(ck, settings)

    print("\n[%s]" % title(check_interrupt))
    check_interrupt(ck, settings)

    print("\n[%s]" % title(check_idle_unload))
    check_idle_unload(ck, settings)

    print("\n[%s]" % title(check_stop_hook_is_safe))
    check_stop_hook_is_safe(ck)

    if do_play:
        print("\n[--play：真出声念一句]")
        sp = speak_mod.Speaker(lambda: settings)
        sp.speak("测试完成，这是本机离线合成的声音。")
        wait_until(lambda: not sp.speaking, 120)

    print("\n%s（%d 项失败）" % ("✅ 全部通过" if not ck.failed else "❌ 有失败",
                              ck.failed))
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())