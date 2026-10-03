# -*- coding: utf-8 -*-
"""配置与数据目录。

对齐原版：录音和凭据只留本机，落在
`~/Library/Application Support/VoicePill/`。
Windows 对应位置：`%LOCALAPPDATA%\\VoicePill\\`。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Optional

APP_DIR_NAME = "VoicePill"

LONG_PRESS_MS = 180


@dataclass(frozen=True)
class KeySpec:
    """一个可绑定的按键。

    两种识别方式，二选一：
      * `scancode` 非空 → 认**扫描码 + E0 标志**。Fn 必须走这条：
        它没有虚拟键映射，vkCode 恒为 0xFF，靠 vk 区分不开。
      * `scancode` 为空 → 认 `vk`。
    """
    key: str
    label: str
    vk: int
    scancode: Optional[int] = None
    extended: bool = False


# 主键候选。默认 Fn —— 2026-10-02 实测本机 Fn 会上报给 OS，
# 且按下/松开成对（tools/fn-probe.py + fn-probe2.log 记录在 docs/移植方案.md 6.2）。
PRIMARY_KEY_CHOICES = {
    "fn": KeySpec("fn", "Fn", vk=0xFF, scancode=0x63, extended=True),
    "rc": KeySpec("rc", "右 Ctrl", vk=0xA3, scancode=0x1D, extended=True),
    "ra": KeySpec("ra", "右 Alt", vk=0xA5, scancode=0x38, extended=True),
    "caps": KeySpec("caps", "Caps Lock", vk=0x14, scancode=0x3A),
}

DEFAULT_PRIMARY_KEY = "fn"


def app_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def recordings_dir() -> str:
    """失败录音留存目录（跨重启保留，供 Retry）。"""
    path = os.path.join(app_dir(), "recordings")
    os.makedirs(path, exist_ok=True)
    return path


def logs_dir() -> str:
    path = os.path.join(app_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def credentials_path() -> str:
    """豆包凭据（freeasr auth 生成）。"""
    return os.path.join(app_dir(), "doubao-credentials.json")


LOCAL_MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


def models_dir() -> str:
    """本地模型的兜底目录（settings 里没指定时用）。"""
    return os.path.join(app_dir(), "models")


def local_model_dir() -> str:
    """本地离线引擎的模型目录。

    优先 settings.json 的 `local_model_dir`，其次环境变量
    `VOICEPILL_LOCAL_MODEL`，最后退回 `%LOCALAPPDATA%\\VoicePill\\models\\<名字>`。
    模型不小（int8 量化版 228 MB），放哪由用户定，所以这里不做猜测。
    """
    configured = Settings.load().local_model_dir.strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    env = os.environ.get("VOICEPILL_LOCAL_MODEL", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(models_dir(), LOCAL_MODEL_NAME)


def project_root() -> str:
    """工程根目录（src/ 的上一层）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bin_dir() -> str:
    """转写引擎二进制所在目录。"""
    return os.path.join(project_root(), "bin")


def engine_path(binary_name: str) -> str:
    """引擎可执行文件完整路径。

    环境变量 `VOICEPILL_ENGINE` 可整体顶替：指向任何实现了 NDJSON 契约的
    可执行文件即可换后端，`bin/` 里没有也不影响。自测用
    `VOICEPILL_ENGINE=.../tools/mock-asr.py` 就能走假引擎跑通全链路。

    不带扩展名的按「引擎二进制」处理，去 `bin/` 找并补 `.exe`；带扩展名的
    （`tools/local-asr.py` 这类脚本引擎）按**工程根目录**解析 —— 它们的源码
    就在工程里，跟 bin/ 下的编译产物不是一回事。
    """
    override = os.environ.get("VOICEPILL_ENGINE")
    if override:
        return override
    if os.path.splitext(binary_name)[1]:
        return os.path.join(project_root(), binary_name.replace("/", os.sep))
    return os.path.join(bin_dir(), binary_name + ".exe")


def engine_argv(binary_path: str) -> list:
    """把引擎路径展开成可执行的 argv 前缀。

    脚本类引擎（`.py` / `.cmd` / `.bat`）不能直接 exec，得用解释器拉起。
    真引擎是 `.exe`，走原样返回那一支，行为不变。
    """
    low = binary_path.lower()
    if low.endswith(".py"):
        return [sys.executable, binary_path]
    if low.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", binary_path]
    return [binary_path]


@dataclass
class Settings:
    provider: str = "local"           # local | doubao | codex
    primary_key: str = DEFAULT_PRIMARY_KEY   # 见 PRIMARY_KEY_CHOICES
    auto_paste: bool = True
    doubao_punctuation: bool = True
    hud_enabled: bool = True
    local_model_dir: str = ""         # 本地离线引擎的模型目录，见 config.local_model_dir()

    # ---- 持久化 ----

    @classmethod
    def path(cls) -> str:
        return os.path.join(app_dir(), "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        s = cls._from_file()
        # 环境变量覆盖，优先级高于文件。只加"测试时非改不可"的那几个。
        #   VOICEPILL_NO_PASTE=1  只把结果打到 stdout，不往当前前台窗口粘。
        #     跑管道自测时必须用：否则假引擎的回显文字会被 Ctrl+V 塞进
        #     用户当时正开着的输入框里，污染人家的内容。
        if os.environ.get("VOICEPILL_NO_PASTE"):
            s.auto_paste = False
        return s

    @classmethod
    def _from_file(cls) -> "Settings":
        p = cls.path()
        if not os.path.isfile(p):
            return cls()
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})
        except (OSError, ValueError, TypeError):
            # 配置损坏不该让程序起不来，退回默认值
            return cls()

    def save(self) -> None:
        with open(self.path(), "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, ensure_ascii=False, indent=2)

    @property
    def primary_spec(self) -> KeySpec:
        """主键定义。配置里写了不认识的 key 就退回默认，不抛异常。"""
        return PRIMARY_KEY_CHOICES.get(
            self.primary_key, PRIMARY_KEY_CHOICES[DEFAULT_PRIMARY_KEY])
