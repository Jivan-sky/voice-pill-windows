# voice-pill-windows

Voice Pill 的 Windows 移植工程。按住热键说话，松手把文字粘到光标处。

| 项 | 值 |
|---|---|
| 上游（原版 macOS） | https://github.com/HA7CH/voice-pill · 0.2.3 · MIT |
| 上游本地克隆 | `D:\FDE_HA7CH\voice-pill`（**只读参考，本工程不改动它**） |
| 本工程 | `D:\Own_tools&skills\voice-pill-windows` |
| 架构拆解（知识库） | `D:\Obsidian_database\04_Projects\Voice-Pill\架构拆解.md` |
| 接口契约（知识库） | `D:\Obsidian_database\03_Knowledge\03-接口_数据\语音转写子进程的流式接口契约.md` |
| 项目档案（知识库） | `D:\Obsidian_database\04_Projects\Voice-Pill\项目档案.md` |
| **本工程镜像（知识库）** | `D:\Obsidian_database\04_Projects\Voice-Pill\Windows移植.md` —— 本目录的库内镜像，**这边改了记得同步过去** |

## 一句话思路

**内核照搬，外壳重写。** ASR 引擎是独立 CLI 子进程（stdin 灌裸 PCM / stdout 出 NDJSON），跨平台可交叉编译；macOS 专属的采音、热键、粘贴、悬浮窗全部重做。

## 当前状态

| 阶段 | 状态 |
|---|---|
| 原版架构拆解 | ✅ 完成（已入库） |
| 可行性评估 | ✅ 完成 |
| 路线选型 | ✅ **B**（Python 轻量壳） |
| 主键定案 | ✅ **Fn** —— 实测本机 Fn 上报为 `E0 63` / vk `0xFF`，按下松开成对 |
| 工具链（M0） | ✅ **完成** —— Go 1.27.0 · cargo 1.99.0 · gcc 16.2.0 · libopus · rustup gnu target |
| Python 依赖 | ✅ venv `.venv` + sounddevice / numpy / scipy |
| 外壳代码 | ✅ 8 个文件写完，`py_compile` 通过，`--check` 端到端跑通 |
| 引擎编译（M2） | ✅ **完成** —— `codex-asr.exe` 10 653 552 B · `freeasr.exe` 24 963 013 B（静态，只依赖系统 DLL） |
| 热键链路 | ✅ **验证通过** —— 进程在 `WinSta0\Default`、钩子+消息泵自激自收正常、物理按键实测收到 134 条 |
| 通路验证（M1） | ✅ **完成（2026-10-02）** —— Fn → 采音 → NDJSON 管道 → 解码 → 粘贴，四段逐段验过，假引擎全链路一次通过（`EXIT CODE 0`） |
| 真实后端 | ❌ **未通** —— 凭据全断（Codex 要 ChatGPT 登录令牌、豆包凭据未生成），与通路本身无关，见 `docs/移植方案.md` 6.4 |

**M1 验收记录（2026-10-02）**

| 环节 | 证据 | 结论 |
|---|---|---|
| 采音 | `ACT stop held=1.95s bytes=64728 wav=64772`（64728+44=64772 自洽） | ✅ 录音落盘字节数对得上 |
| 管道 | `pipe-selftest` 回放 127 224 B / 2.65 s | ✅ 背压、解码、超时、退出码全走通 |
| 粘贴 | `paste-selftest` 文字落入靶子文本框，剪贴板已还原 | ✅ 真 `paste.paste()` 生效 |
| 全链路 | `main.py --once`（假引擎）一次录音后 `EXIT CODE 0` | ✅ 含进程生命周期 |

> **`--once` 跑完不退出？** 踩过一次，进程挂了 3 分钟还占着麦克风（见 `docs/移植方案.md` 5.2）。调试期一律包一层 `timeout`：
> ```bash
> timeout 150 .venv/Scripts/python.exe src/main.py --once
> ```
> 后遗症很隐蔽：僵尸进程握着麦克风时，**新起的实例流能正常打开、不报错，但一帧都收不到**（5.3）。现在 `on_stop()` 里有体检闸门，采到不足 0.10 秒会直接提示"麦克风没出声"，不再把空音频灌给后端。

> **别再误判「0 条事件 = 环境限制」。** 早先几轮自测收到 0 条按键，一度归因于"后台进程看不见输入"。现在已定论：**进程没问题**，是那 60 秒里确实没人按键盘。判据本身缺失，不是环境限制。详见 `docs/移植方案.md` 6.3。
>
> 要测 Fn 这种"得等人来按"的项，用 `tools/fn-watch.py` 长时监听（默认 900 秒，随时按都算数），**别用固定窗口**——实测 60 秒窗口里人往往在往输入框打字，134 行字母键会把要看的 Fn 淹掉。

## 目录

| 路径 | 内容 |
|---|---|
| `src/` | Python 外壳：热键、采音、管道、粘贴、悬浮条、状态机 |
| `bin/` | 两个 ASR 引擎可执行文件放这里（`codex-asr.exe` / `freeasr.exe`），见 `bin/README.md` |
| `vendor/` | 两个引擎的上游源码副本（`codex-asr` / `FreeASR`），与 `D:\FDE_HA7CH\voice-pill` 里的一致 |
| `tools/build-engines.sh` | 一键编两个引擎，四个坑都注释在脚本里 |
| `tools/fn-probe.py` | Fn 键探针（Raw Input + 低级钩子双通道），带 `--keyup` / `--only` 开关 |
| `tools/desktop-probe.py` | 排查"收不到键盘"：窗口站/桌面/完整性 + 钩子自激自收 |
| `tools/input-liveness.py` | 判会话里到底有没有物理输入（`GetLastInputInfo`，动鼠标即可，不用按键） |
| `tools/fn-watch.py` | 长时只盯 Fn 的监听（默认 900 秒），输出是给上层读的事件流 |
| `tools/shell-selftest.py` | 热键 + 采音自测，**不需要引擎** |
| `tools/capture-probe.py` | 采音单独验：开流耗时 + 收尾竞态回归 + 空录音闸门 + 对象复用 |
| `tools/pipe-selftest.py` | 拿 WAV 直接喂引擎，跳过麦克风和热键，单独验管道/解码/超时 |
| `tools/paste-selftest.py` | 自建靶子文本框验粘贴（不污染用户正在用的窗口），含剪贴板还原 |
| `tools/mock-asr.py` | 假引擎（实现 NDJSON 契约），用来单独验管道/解码/粘贴 |
| `src/console.py` | 控制台编码兜底（管道下打印 ✅ 会 GBK 崩） |
| `docs/移植方案.md` | 路线对比、键位实测记录、里程碑、风险 |
| `fn-probe.log` / `fn-probe2.log` | 探针原始日志（第一轮全键、第二轮只看 Fn 的按下松开） |

## 快速开始

```bash
# 自检：引擎、麦克风、配置、凭据
.venv\Scripts\python.exe src\main.py --check

# 正常运行
.venv\Scripts\python.exe src\main.py
```

跑之前先把两个引擎二进制放进 `bin/`（见 `bin/README.md`）。

## 许可

原版为 MIT。移植产物沿用 MIT，保留上游署名与第三方组件许可（`D:\FDE_HA7CH\voice-pill\THIRD_PARTY.md`）。
