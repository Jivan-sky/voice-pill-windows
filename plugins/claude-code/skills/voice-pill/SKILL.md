---
name: voice-pill
description: Use when the user dictates with Voice Pill (presses Fn) or asks to speak instead of type. Covers reading back what they said and speaking a reply out loud.
---

# Voice Pill（Claude Code 侧）

按下 Fn 说话、松手，文字自动落到光标处；转写在**本机**完成，音频不出机器。
本插件把这份能力接给 Claude Code，只是控制面，不重造语音。

**这份技能与 Codex 侧那份是同一个能力的两套宿主接线**，语义以
`docs/CONTRACT.md` 为准；两边哪句对不上，以契约与代码为准，不以本文为准。

## 人设：单一真相源

人格定位写在 **`persona.md`**（源在本仓库 `plugins/voice-pill/persona.md`，
安装时渲染进本插件根目录）。**用它之前先读那一份**，不要在这里复述——
双源手抄必然漂移。本文件只讲「怎么调这个技能」。

## 什么时候用它

- 用户说「我刚刚说的话」「听一下」「我说了」——他们已经按下 Fn 说完了，
  内容在待取队列里：调 `voice_pill_take`。
- 用户希望你**请他说**：调 `voice_pill_listen`。
- 录音中途要中断：`voice_pill_cancel`。
- 确认引擎活不活：`voice_pill_status`。
- 用户要**听**你说、或问「能不能念出来」：`voice_pill_speak`。

## 关键语义

- `voice_pill_take` 是**取走**语义，取过一次就没了。待取文字有保鲜期
  （默认 30 分钟）：更早说的已作废、取不回来，用户问起时如实说，别反复调。
- 引擎跟着宿主起落：开新会话时会自动把它拉起来（`SessionStart` 钩子）。
  所以「引擎没在跑」通常意味着刚关过宿主——请用户重开即可，别让他手工跑启动命令。
- `voice_pill_speak` 是**本机**合成（离线）。代码块、URL、路径直接丢给它没有意义：
  它会先把代码块换成「（代码块略）」、链接只留标题。想让它念得准，给它能听懂的句子。

## 两个宿主同时挂着时

`take` 是**取走即消费**：Codex 与 Claude Code 谁先问、字就归谁，不会被投喂两遍。
所以用户如果两边都开着，别对「我这边没取到字」下结论——可能已经被另一边取走了。

## 不要做的事

- 不要把 `voice_pill_listen` 当默认交互：多数时候用户自己按 Fn 更顺。
- 不要每轮都调 `voice_pill_status`。
- 不要在用户没说、你也没问的情况下擅自开录音。
- 不要每轮都念。默认 `speak_replies=false` 就是「按需」的意思——每轮都念是广播，
  不是对话。
