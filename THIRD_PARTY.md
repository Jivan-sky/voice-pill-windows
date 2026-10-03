# Third-party notices

`voice-pill-windows` 的移植代码沿用上游的 MIT 许可（© 2026 HA7CH）。下面每一项都保留
原署名与许可。本文件是这份清单**在本仓库内的正本**；上游 macOS 版的对应文件在
`D:\FDE_HA7CH\voice-pill\THIRD_PARTY.md`。

| 组件 | 来源 / 版本 | 许可与用法 |
| --- | --- | --- |
| Voice Pill（原版 macOS） | https://github.com/HA7CH/voice-pill · 0.2.3 | MIT · © 2026 HA7CH；本工程是它的 Windows 移植 |
| LiquidType HUD / 玻璃 / 字幕 / 波形 | https://github.com/LuliYanng/LiquidType（03d5659…） | MIT；上游的 HUD 设计源自它。本工程 `src/hud.py` 是用 tkinter **重写**（无玻璃效果），未拷贝其代码 |
| PasteController（剪贴板快照 → 还原 → 发键前校验） | https://github.com/simicvm/whisper（45a497c…） | MIT；经上游转写。本工程 `src/paste.py` 按同一套语义重写 |
| FreeASR（豆包引擎） | https://github.com/WEIFENG2333/FreeASR（252f6387…） | 上游 README 声明 MIT，副本在 `vendor/FreeASR/`。上游注明豆包走的是**非官方**输入法协议、定位学习研究而非商用；这不构成对豆包服务的访问许可 |
| libopus | https://opus-codec.org/ | BSD 风格；静态链接进 `bin/freeasr.exe`。完整文本见 https://opus-codec.org/license/ |
| codex-asr 流式分支 | https://github.com/Wangnov/codex-asr（479f6a7）+ 本地流式实现 | MIT；副本在 `vendor/codex-asr/`（含 `LICENSE-MIT`），编译产物 `bin/codex-asr.exe` |
| Rust crates | `vendor/codex-asr/Cargo.lock` | 各 crate 自带 LICENSE / COPYING / NOTICE |
| Go modules | `vendor/FreeASR/go.mod`、`go.sum` | 同上 |
| sherpa-onnx | https://github.com/k2-fsa/sherpa-onnx | Apache-2.0；`local` 后端经 Python 包 `sherpa-onnx` 调用 |
| SenseVoice-Small int8 模型 | https://github.com/FunAudioLLM/SenseVoice ；镜像 `WEAAEW/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`（ModelScope） | 许可见上游模型卡。**模型权重不随本仓库分发**，由使用者自行下载并写进 `settings.json` 的 `local_model_dir` |
| PortAudio / python-sounddevice | https://www.portaudio.com/ · https://github.com/spatialaudio/python-sounddevice | MIT 风格（PortAudio）；sounddevice 为 MIT |
| numpy / scipy | https://numpy.org/ · https://scipy.org/ | BSD-3-Clause |
| tkinter / Tcl-Tk | https://www.tcl.tk/ | BSD 风格；随 CPython 分发 |

上游清单里的 **ANC 音频桥** 是 macOS 专属，本移植未使用。

本仓库**不分发**：豆包凭据、Codex 令牌、FFmpeg、SenseVoice 模型权重、任何签名密钥。
本工程与 Apple、字节跳动、腾讯、OpenAI、k2-fsa 均无隶属关系。
