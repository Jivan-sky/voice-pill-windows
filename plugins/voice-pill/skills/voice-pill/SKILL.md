---
name: voice-pill
description: Use when the user dictates with Voice Pill (presses Fn) or asks to speak instead of type. Covers reading back what they said and recording a segment on request.
---

# Voice Pill

按下 Fn 说话、松手，文字自动落到光标处；转写在**本机**完成，音频不出机器。
插件把这份能力接给 Codex，只是控制面，不重造语音。

## 人设：单一真相源

本插件的人格定位在 **`persona.md`**——照 ANC（`HA7CH/ai-native-company`
`SPEC.md` §4.1）的**七段式 schema** 书写。**用它之前先读那一份**，
不要在这里、也不要在别处复述：ANC 把「双源手抄」列为头号架构债，
手抄过的口径必然漂移。本文件只讲「怎么调这个技能」。

## 什么时候用它

- 用户说「我刚刚说的话」「听一下」「我说了」——他们已经按下 Fn 说完了，
  内容在待取队列里：调 `voice_pill_take`。
- 用户希望你**请他说**：调 `voice_pill_listen`，它会录音并在结束后把文字给你。
- 用户在录的时候想中断：`voice_pill_cancel`。
- 要先确认引擎活不活：`voice_pill_status`。
- 用户想**听**你说、或问「能不能念出来」「念一下」：`voice_pill_speak`（把要说的话
  交给它）。正在念的时候用户开口或改主意：`voice_pill_shutup`。

## 关键语义

- `voice_pill_take` 是**取走**语义，取过一次就没了，不要为了「再确认一下」
  重复调用。待取文字有保鲜期（默认 30 分钟）：更早说的已经作废，取不回来，
  用户问起时如实说，别反复调。
- `voice_pill_listen` 的 `seconds` 是自动结束时间；用户自己按 Fn 松手会提前结束。
  返回的 `text` 为空通常是这段没人说话，或麦克风没出声——不要当成错误重试，
  先问用户。
- 引擎跟着 Codex 起落：开新会话时会自动把它拉起来（`SessionStart` 钩子），关掉 Codex 它会自己收摊。所以「引擎没在跑」通常意味着用户刚关过 Codex —— 请用户重开 Codex 即可，不要让他去手工跑启动命令。
- 引擎没在跑时 `voice_pill_listen` 也会自动拉起它，不需要用户手动启动。
- `voice_pill_speak` 是**本机**合成（离线，不联网），念出来的东西只去扬声器。
  代码块、URL、路径直接丢给它没有意义：它会先把代码块换成「（代码块略）」、
  链接只留标题。想让它念得准，给它**能听懂的句子**。

## 不要做的事

- 不要把 `voice_pill_listen` 当默认交互：多数时候用户自己按 Fn 更顺，
  只在用户明确希望你请他说话时才用。
- 不要每轮都调 `voice_pill_status`。
- 不要在用户没说、你也没问的情况下擅自开录音。
- 不要每轮都念。默认 `speak_replies=false` 就是「按需」的意思——只有用户明确要听，
  或用户请你念，才调 `voice_pill_speak`。每轮都念是广播，不是对话。
