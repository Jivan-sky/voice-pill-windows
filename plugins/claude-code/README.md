# Voice Pill —— Claude Code 侧接线

同一份 `voicepill.exe`，第二家宿主。接口语义不在这里另写一份，见
[`docs/CONTRACT.md`](../../docs/CONTRACT.md)；Codex 侧的接线在
[`plugins/voice-pill/`](../voice-pill/)。

## 三个钩子接哪里

| 钩子 | 干什么 | 契约动词 |
|---|---|---|
| `SessionStart` | 写锚点纸条、确保看门狗在跑、等引擎就绪 | `status`（轮询问就绪） |
| `UserPromptSubmit` | 把用户刚说的话注入本轮上下文 | `take` |
| `Stop` | 把助手这一轮的正文交给本机念出来 | `speak`（`auto=true`） |

产物是**同一条 exe 的三个子命令**：`voicepill.exe session-start /
prompt-submit / stop`。CC 与 Codex 两侧调的是同一份实现——这正是契约
要的效果，接第二家不需要把桥再写一遍。

**这里没有适配层，是核过的**：CC 的 Stop 钩子载荷里**确实带
`last_assistant_message`**（CC 二进制内的 schema 原文：*"Text content of the
last assistant message before stopping. Avoids the need to read and parse the
transcript file."*），而 `hooks.go` 的 `stop` 读的就是这个字段名。
`UserPromptSubmit` 那边，CC 与 Codex 的 `hookSpecificOutput` /
`additionalContext` 形状也一致。

## 比 Codex 侧少一层麻烦

Codex 那边的钩子命令必须避开仓库路径里的 `&`（未加引号会被 cmd 当分隔符
拆断，加了引号 Codex 只回一句 Completed、脚本根本没跑——两种都坏），所以
`plugins/install.py` 才要建目录联接把插件搬到 `~/plugins/voice-pill`。

**CC 不用**：CC 的钩子命令是**直接 spawn、不过 shell**（CC 二进制原文：
*"resolved as an executable and spawned directly with these arguments — no
shell. Path placeholders like `${CLAUDE_PLUGIN_ROOT}` are substituted
per-element as plain strings, so paths with quotes, `$`, or backticks never
reach a shell parser."*）。所以这里直接用 `${CLAUDE_PLUGIN_ROOT}`，不需要
联接、也不需要绝对路径模板。

## 装之前要做的两步

1. **放 exe**：把 `bin\voicepill.exe` 拷到本目录（`plugins/claude-code/voicepill.exe`）。
   它被 `.gitignore` 挡住，不进仓库——和 Codex 侧同一个规矩。
2. **渲染 persona.md**：人设的源只有一份，在 `plugins/voice-pill/persona.md`；
   本插件根目录那份是**安装时渲染的副本**（同样被 ignore）。别在这里手抄。

然后按 CC 自己的插件流程装：本仓库根目录的
[`.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json)
把一个插件（`source: ./plugins/claude-code`）登在市场里。

## 验收（端到端两条，来自契约 §5）

1. **只改宿主侧配置**，CC 就能「说一句 → 文字进本轮上下文」；
2. 同一份 exe 同时服务两个宿主，**不重复投喂**（靠 `take` 的取走语义）。

## 状态

**未装、未跑过**。本目录目前是接线文件本身；上面两条验收还没做，
`voicepill.exe` 也还没放进本目录。**不要在没跑过验收的情况下说它已经能用。**
