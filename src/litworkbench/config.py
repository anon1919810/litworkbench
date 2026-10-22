"""可编辑的 TOML 配置。

配置里只保存「环境变量名」，不保存密钥本身。``redact_config`` 用于
``doctor``、任务快照与导出，确保任何位置都不会落下密钥。
"""

from __future__ import annotations

import tomllib
import json
from pathlib import Path
from typing import Any

from litworkbench.errors import LitError

CONFIG_FILENAME = "config.toml"

DEFAULT_CONFIG: dict[str, Any] = {
    "workspace": {
        "sources_dir": "sources",
        "artifacts_dir": "artifacts",
        "db_file": "litworkbench.sqlite3",
    },
    "ocr": {
        # auto：按页判断；off：只用文本层；force：所有页强制 OCR
        "mode": "auto",
        "render_scale": 2.0,
        "min_text_chars": 20,
        "engine": "rapidocr",
    },
    "provider": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "local-model",
        "api_key_env": "LITWORKBENCH_API_KEY",
        "timeout": 60.0,
        "temperature": 0.0,
        "max_output_tokens": 2048,
    },
    "agent": {
        "max_steps": 12,
        "allowed_tools": ["search", "read", "excerpt", "finish"],
        "search_limit": 8,
        "default_profile": "heritage",
    },
}

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = {key: dict(value) if isinstance(value, dict) else value for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict[str, Any]:
    """读取配置；不存在时返回默认配置。"""
    if not path.exists():
        return _deep_merge(DEFAULT_CONFIG, {})
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - 依赖用户输入
        raise LitError(f"配置文件解析失败（{path.name}）：{exc}") from exc
    return _deep_merge(DEFAULT_CONFIG, data)


#: 生成配置时附带的说明（不影响往返读取）
COMMENTS: dict[str, str] = {
    "workspace.sources_dir": "不可变原件副本目录（按内容哈希存放）",
    "workspace.artifacts_dir": "页图、报告等派生文件目录",
    "workspace.db_file": "SQLite 数据库文件名",
    "ocr.mode": "auto = 逐页判断是否需要 OCR；off = 只用文本层；force = 全部页强制 OCR",
    "ocr.render_scale": "PDF 渲染倍率（2.0 约为 144 DPI）",
    "ocr.min_text_chars": "文本层有效字数低于该值时判定为扫描页并进入 OCR",
    "ocr.engine": "OCR 引擎名称（当前仅支持 rapidocr）",
    "provider.base_url": "任何 OpenAI 兼容的 /chat/completions 服务（本地或远程）",
    "provider.model": "模型名",
    "provider.api_key_env": "存放密钥的环境变量名；程序不会自动读取本机其他应用的凭据",
    "provider.timeout": "单次请求超时（秒）",
    "provider.temperature": "采样温度",
    "provider.max_output_tokens": "单次回复的最大 token 数",
    "agent.max_steps": "单个研究任务的最大步骤数",
    "agent.allowed_tools": "允许模型调用的内置动作（不开放任意 shell/文件/网络）",
    "agent.search_limit": "search 动作默认返回条数",
    "agent.default_profile": "未指定 --profile 时使用的研究方案",
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{" + inner + "}"
    return json.dumps(str(value), ensure_ascii=False)


def render_config_toml(config: dict[str, Any]) -> str:
    """把配置序列化成 TOML 文本（``save_config`` 使用，保证往返一致）。"""
    merged = redact_config(config)
    lines = [
        "# 本地文献工作台配置（可直接编辑）",
        "# 密钥只写环境变量名；真实密钥请放在环境变量里，不要写进本文件。",
        "",
    ]
    for section, values in merged.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            comment = COMMENTS.get(f"{section}.{key}")
            if comment:
                lines.append(f"# {comment}")
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """返回可安全展示/持久化的配置副本。"""
    safe = _deep_merge(DEFAULT_CONFIG, config)
    provider = safe.get("provider", {})
    # 明确写死：配置里不应该出现任何密钥字段
    provider.pop("api_key", None)
    provider.pop("key", None)
    provider.pop("token", None)
    return safe


def provider_settings(config: dict[str, Any]) -> dict[str, Any]:
    provider = dict(config.get("provider", {}))
    provider.pop("api_key", None)
    provider.pop("key", None)
    provider.pop("token", None)
    return provider
