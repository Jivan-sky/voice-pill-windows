---
name: voice-pill
description: Use when the user dictates with Voice Pill (presses Fn) or asks to speak instead of type. Covers reading back what they said and recording a segment on request.
---

# Voice Pill

按下 Fn 说话、松手，文字自动落到光标处；转写在**本机**完成，音频不出机器。
插件把这份能力接给 Codex，只是控制面，不重造语音。

## 什么时候用它

- 用户说「我刚刚说的话」「听一下」「我说了」——他们已经按下 Fn 说完了，
  内容在待取队列里：调 `voice_pill_take`。
- 用户希望你**请他说**：调 `voice_pill_listen`，它会录音并在结束后把文字给你。
- 用户在录的时候想中断：`voice_pill_cancel`。
- 要先确认引擎活不活：`voice_pill_status`。

## 关键语义

- `voice_pill_take` 是**取走**语义，取过一次就没了，不要为了「再确认一下」
  重复调用。待取文字有保鲜期（默认 30 分钟）：更早说的已经作废，取不回来，
  用户问起时如实说，别反复调。
- `voice_pill_listen` 的 `seconds` 是自动结束时间；用户自己按 Fn 松手会提前结束。
  返回的 `text` 为空通常是这段没人说话，或麦克风没出声——不要当成错误重试，
  先问用户。
- 引擎没在跑时 `voice_pill_listen` 会自动拉起它，不需要用户手动启动。

## 不要做的事

- 不要把 `voice_pill_listen` 当默认交互：多数时候用户自己按 Fn 更顺，
  只在用户明确希望你请他说话时才用。
- 不要每轮都调 `voice_pill_status`。
- 不要在用户没说、你也没问的情况下擅自开录音。
