# -*- coding: utf-8 -*-
"""打印 sid → 音色名 的对应表（Kokoro 多语言 v1.1-zh），并可实测校验。

名字从哪来
----------
上游 `hexgrad/Kokoro-82M-v1.1-zh` 的 `voices/` 目录**顺序就是 sid 顺序**：
0-2 英语，3-57 中文女声 `zf_*`，58-102 中文男声 `zm_*`，共 103。
`voices.bin` 只存向量、不存名字——所以这张表是「顺序 + 数目」推出来的。
推断不能只靠推断，`--probe` 把它验成事实。

怎么验
------
1. **数目自检**：3 英语 + 55 `zf_` + 45 `zm_` = 103，必须与模型自称的说话人数相等。
2. **`--probe` 实测基频**：逐 sid 合成一句、估基频，女声区/男声区的分界线
   必须正好落在 57/58。换模型或换错包时这条会立刻报警。
3. **受控对照**（人工做一次就够，见 `docs/移植方案.md` 16.3）：拿上游自带的
   `samples/HEARME_zf_001.wav` 第一句，与本地 sid=3 同句同速合成比对 F0。

用法
----
    .venv\\Scripts\\python.exe tools\\tts-voice-map.py            # 打表
    .venv\\Scripts\\python.exe tools\\tts-voice-map.py --probe    # 打表 + 实测基频
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))
import console                     # noqa: E402

console.make_output_safe()

# 顺序照抄上游 voices/ 目录（逐项可比对，不改顺序）
EN = ["af_maple", "af_sol", "bf_vale"]
ZF = [
    "zf_001", "zf_002", "zf_003", "zf_004", "zf_005", "zf_006", "zf_007", "zf_008",
    "zf_017", "zf_018", "zf_019", "zf_021", "zf_022", "zf_023", "zf_024", "zf_026",
    "zf_027", "zf_028", "zf_032", "zf_036", "zf_038", "zf_039", "zf_040", "zf_042",
    "zf_043", "zf_044", "zf_046", "zf_047", "zf_048", "zf_049", "zf_051", "zf_059",
    "zf_060", "zf_067", "zf_070", "zf_071", "zf_072", "zf_073", "zf_074", "zf_075",
    "zf_076", "zf_077", "zf_078", "zf_079", "zf_083", "zf_084", "zf_085", "zf_086",
    "zf_087", "zf_088", "zf_090", "zf_092", "zf_093", "zf_094", "zf_099",
]
ZM = [
    "zm_009", "zm_010", "zm_011", "zm_012", "zm_013", "zm_014", "zm_015", "zm_016",
    "zm_020", "zm_025", "zm_029", "zm_030", "zm_031", "zm_033", "zm_034", "zm_035",
    "zm_037", "zm_041", "zm_045", "zm_050", "zm_052", "zm_053", "zm_054", "zm_055",
    "zm_056", "zm_057", "zm_058", "zm_061", "zm_062", "zm_063", "zm_064", "zm_065",
    "zm_066", "zm_068", "zm_069", "zm_080", "zm_081", "zm_082", "zm_089", "zm_091",
    "zm_095", "zm_096", "zm_097", "zm_098", "zm_100",
]

TOTAL = len(EN) + len(ZF) + len(ZM)
FEMALE_RANGE = (len(EN), len(EN) + len(ZF) - 1)        # (3, 57)
MALE_RANGE = (FEMALE_RANGE[1] + 1, TOTAL - 1)          # (58, 102)

NAME_BY_SID = {}
for _i, _n in enumerate(EN):
    NAME_BY_SID[_i] = _n
for _i, _n in enumerate(ZF):
    NAME_BY_SID[len(EN) + _i] = _n
for _i, _n in enumerate(ZM):
    NAME_BY_SID[len(EN) + len(ZF) + _i] = _n


def gender(sid: int) -> str:
    if FEMALE_RANGE[0] <= sid <= FEMALE_RANGE[1]:
        return "女"
    if MALE_RANGE[0] <= sid <= MALE_RANGE[1]:
        return "男"
    return "英"


def table() -> str:
    lines = ["sid  性别 音色名", "---  ---- ----------"]
    for sid in range(TOTAL):
        lines.append("%-3d  %s    %s" % (sid, gender(sid), NAME_BY_SID[sid]))
    return "\n".join(lines)


def median_f0(samples, rate) -> float:
    """自相关估基频：只取有声帧，取中位数。够用来分男女。"""
    import numpy as np
    x = np.asarray(samples, dtype="float64")
    frame = int(rate * 0.04)
    hop = int(rate * 0.02)
    f0s = []
    for i in range(0, len(x) - frame, hop):
        seg = x[i:i + frame]
        if (seg ** 2).mean() ** 0.5 < 0.02:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[frame - 1:]
        lo, hi = int(rate / 400.0), int(rate / 60.0)
        if hi >= len(ac):
            continue
        pk = int(np.argmax(ac[lo:hi])) + lo
        if pk > 0 and ac[pk] >= 0.3 * ac[0]:
            f0s.append(rate / pk)
    return float(np.median(f0s)) if f0s else 0.0


def probe(settings, config_module) -> int:
    """逐 sid 合成一句、量基频，检查男女分界是否落在 57/58。"""
    import sherpa_onnx
    import speak as speak_mod       # 蹭它的 fd2 静音：C++ 那句 Unknown token 是无害噪音
    root = config_module.tts_model_dir()
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=os.path.join(root, "model.onnx"),
                voices=os.path.join(root, "voices.bin"),
                tokens=os.path.join(root, "tokens.txt"),
                data_dir=os.path.join(root, "espeak-ng-data"),
                lexicon=",".join([os.path.join(root, "lexicon-us-en.txt"),
                                  os.path.join(root, "lexicon-zh.txt")]),
            ),
            num_threads=max(1, int(settings.tts_threads)),
        ),
    )
    if not cfg.validate():
        print("TTS 配置校验不过：%s" % root)
        return 1
    with speak_mod._native_stderr_silenced():
        tts = sherpa_onnx.OfflineTts(cfg)
    n = tts.num_speakers
    print("\n模型自称说话人数 = %d；本表 = %d %s"
          % (n, TOTAL, "✅" if n == TOTAL else "❌ 数目对不上，映射已失效"))
    fails = []
    if n != TOTAL:
        fails.append("说话人数 %d != 表 %d" % (n, TOTAL))
    f0 = {}
    for sid in range(min(n, TOTAL)):
        with speak_mod._native_stderr_silenced():
            a = tts.generate(text="今天天气不错。", sid=sid, speed=1.0)
        f0[sid] = median_f0(a.samples, a.sample_rate)
    fem = [f0[s] for s in range(*[FEMALE_RANGE[0], FEMALE_RANGE[1] + 1])]
    mal = [f0[s] for s in range(*[MALE_RANGE[0], MALE_RANGE[1] + 1])]
    print("女声区 sid %d-%d  F0 中位 %.0f  区间 %.0f~%.0f"
          % (FEMALE_RANGE[0], FEMALE_RANGE[1],
             sorted(fem)[len(fem) // 2], min(fem), max(fem)))
    print("男声区 sid %d-%d  F0 中位 %.0f  区间 %.0f~%.0f"
          % (MALE_RANGE[0], MALE_RANGE[1],
             sorted(mal)[len(mal) // 2], min(mal), max(mal)))
    # 分界线：57 必须比 58 高（女声基频高于男声）。反过来就是映射错位。
    if not f0.get(FEMALE_RANGE[1], 0) > f0.get(MALE_RANGE[0], 0):
        fails.append("57/58 分界线不成立：sid57=%.0fHz sid58=%.0fHz"
                     % (f0.get(FEMALE_RANGE[1], 0), f0.get(MALE_RANGE[0], 0)))
    print("分界线 sid%d=%.0fHz vs sid%d=%.0fHz %s"
          % (FEMALE_RANGE[1], f0.get(FEMALE_RANGE[1], 0),
             MALE_RANGE[0], f0.get(MALE_RANGE[0], 0),
             "✅" if not fails else "❌"))
    for msg in fails:
        print("❌ %s" % msg)
    return 1 if fails else 0


def main() -> int:
    print(table())
    if "--probe" not in sys.argv[1:]:
        print("\n（加 --probe 会真的加载模型，逐 sid 实测基频来校验这张表）")
        return 0
    try:
        import config as config_module
    except ImportError as exc:
        print("导入 config 失败：%r（要在工程 venv 里跑）" % (exc,))
        return 1
    return probe(config_module.Settings.load(), config_module)


if __name__ == "__main__":
    raise SystemExit(main())