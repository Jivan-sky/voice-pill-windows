# -*- coding: utf-8 -*-
"""转写后端参数表。

对齐原版 macOS 的 Sources/LiveProtocol.swift:3-15。**参数值不要擅自改**——
它们是与引擎二进制的既有契约，改了就不通。

契约（详见知识库「语音转写子进程的流式接口契约」）：
    输入  stdin  裸 PCM，小端 16 位有符号，单声道，无容器
    输出  stdout 按行分隔 JSON，字段 type + text
    type  partial / final / result / error；text 是整句快照，不是增量
"""
from __future__ import annotations


from dataclasses import dataclass


def _default_credential_path() -> str:
    """豆包凭据落盘位置（对齐原版 ~/Library/Application Support/VoicePill/ 的语义）。

    路径的唯一定义在 config.credentials_path()——这里转一手，免得两处各写一遍
    日后改目录时漏掉一个。
    """
    import config
    return config.credentials_path()


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    binary: str
    sample_rate: int
    completion_event: str
    finish_timeout: float

    def arguments(self, punctuation: bool = True) -> list[str]:
        raise NotImplementedError


@dataclass(frozen=True)
class CodexProvider(Provider):
    """Codex 实时听写。复用本机 ~/.codex/auth.json，不需要 API Key。"""

    def arguments(self, punctuation: bool = True) -> list[str]:
        # 原版：["stream", "--sample-rate", "24000"]。标点由服务端生成，无开关。
        return ["stream", "--sample-rate", str(self.sample_rate)]


@dataclass(frozen=True)
class DoubaoProvider(Provider):
    """豆包 FreeASR。走非官方输入法协议，上游定位为学习研究用途。"""

    credential_path: str = ""

    def arguments(self, punctuation: bool = True) -> list[str]:
        # 原版：["live", "--quiet", "--credential-path", <path>]，再按需追加 --no-punctuation
        args = [
            "live",
            "--quiet",
            "--credential-path",
            self.credential_path or _default_credential_path(),
        ]
        if not punctuation:
            args.append("--no-punctuation")
        return args


CODEX = CodexProvider(
    key="codex",
    label="Codex · 实时字幕",
    binary="codex-asr",
    sample_rate=24000,
    completion_event="result",   # 注意：Codex 的 final 只是「一句话结束」，result 才是会话结束
    finish_timeout=65.0,
)

DOUBAO = DoubaoProvider(
    key="doubao",
    label="豆包 · 实时字幕",
    binary="freeasr",
    sample_rate=16000,
    completion_event="final",
    finish_timeout=35.0,
    credential_path="",
)

PROVIDERS = {p.key: p for p in (CODEX, DOUBAO)}
DEFAULT_PROVIDER = "codex"


def get(key: str) -> Provider:
    """按 key 取后端；未知 key 退回默认，不抛异常（对齐原版 `?? "codex"` 的容错）。"""
    return PROVIDERS.get(key, CODEX)
