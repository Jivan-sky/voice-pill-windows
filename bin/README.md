# bin/ —— 转写引擎放这里

外壳（`src/`）只负责录音、管道、粘贴；**真正干活的两个 ASR 引擎是独立子进程**，
可执行文件就放本目录。外壳按文件名找它们，见 `src/config.py: engine_path()`。

| 文件 | 来源 | 用途 | 采样率 | 完成事件 |
|---|---|---|---|---|
| `codex-asr.exe` | `voice-pill/vendor/codex-asr`（Rust） | Codex 实时字幕 | 24000 Hz | `result` |
| `freeasr.exe` | `voice-pill/vendor/FreeASR`（Go） | 豆包实时字幕 | 16000 Hz | `final` |

## 进程契约（两边必须一致，见 docs/移植方案.md）

* **stdin**：裸 PCM16LE 单声道，**没有 WAV 头、没有容器**。
* **stdout**：一行一个 JSON（NDJSON），`{"type":"partial|final|result|error","text":"..."}`。
* `text` 是**整句快照，不是增量**——收到就整体覆盖，不要拼接。
* 成功判据三连：进程退出码为 0 **且** 没有未消费的尾部缓冲 **且** 完成文本非空。

## 为什么必须自己编

上游 release 里的 `codex-asr 0.1.3-stream.1` 是**未发布的实验分支**；
`main.rs` 里根本没有 `stream` 子命令。想在 Windows 上拿到实时字幕，
只能拿 vendored 源码自己编。

## 编译方式

见 `docs/移植方案.md` 的 M2。要点：

* Rust → 用 `x86_64-pc-windows-gnu` 目标（本机没有 MSVC 链接器，
  且 MSYS2 的 `/usr/bin/link.exe` 是 coreutils 的硬链接工具，
  会**冒充 MSVC 链接器**把 msvc 目标的构建搞成莫名其妙的错误）。
* Go → `CGO_ENABLED=1`，需要 `libopus`（MSYS2 包 `mingw-w64-x86_64-opus`）。

## 第三个引擎：本地离线（**不在本目录**）

`local` 后端用的是 `tools/local-asr.py`——Python 脚本，不是编译产物，所以
留在工程里，由 `config.engine_path()` 按**工程根目录**解析（带扩展名的不去
`bin/` 找）。它跑 sherpa-onnx + SenseVoice，16000 Hz、完成事件 `final`，
与上面两个同理；差别是它不联网、不要账号。

模型目录见 `settings.json` 的 `local_model_dir`，没配就退回
`%LOCALAPPDATA%\VoicePill\models\<模型名>`。下载与实测见 `docs/移植方案.md` 6.4。
