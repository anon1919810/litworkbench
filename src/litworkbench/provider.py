"""OpenAI 兼容 chat/completions 客户端。

只做一件事：把消息发给用户自己配置的 ``base_url``，拿回文本。

安全约束：

* 密钥只从**环境变量**读取，不读取本机其他应用的凭据；
* 错误信息里**不出现**密钥、请求头或服务端响应体，只给状态码与错误类型；
* 配置缺失时直接报错，不发出任何网络请求。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from litworkbench.errors import ProviderError, ProviderTimeoutError

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"}


def _is_loopback(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in LOOPBACK_HOSTS


class ChatProvider:
    """按配置调用 OpenAI 兼容接口。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = dict(config or {})
        self.base_url = str(config.get("base_url") or "").strip().rstrip("/")
        self.model = str(config.get("model") or "").strip()
        self.api_key_env = str(config.get("api_key_env") or "").strip()
        self.timeout = float(config.get("timeout") or 60.0)
        self.temperature = float(config.get("temperature") or 0.0)
        self.max_output_tokens = int(config.get("max_output_tokens") or 2048)

    # ------------------------------------------------------------- 预检

    def api_key(self) -> str:
        if not self.api_key_env:
            raise ProviderError(
                "未配置密钥：请在 config.toml 的 provider.api_key_env 中填写环境变量名。"
            )
        value = os.environ.get(self.api_key_env, "").strip()
        if not value:
            raise ProviderError(
                f"环境变量 {self.api_key_env} 未设置或为空；程序不会自动读取本机其他凭据。"
            )
        return value

    def check(self) -> None:
        """发送请求前的本地检查（不触网）。"""
        if not self.base_url:
            raise ProviderError("未配置 provider.base_url：请在 config.toml 中填写模型服务地址。")
        if not self.model:
            raise ProviderError("未配置 provider.model：请在 config.toml 中填写模型名。")
        self.api_key()

    def describe(self) -> dict[str, Any]:
        """可安全展示/写盘的配置摘要（永远不含密钥）。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "timeout": self.timeout,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }

    # ------------------------------------------------------------- 调用

    def complete(self, messages: list[dict[str, Any]]) -> str:
        self.check()
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_output_tokens:
            payload["max_tokens"] = self.max_output_tokens
        headers = {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        # 本机环回地址不经过环境代理（否则会被代理拦截成 502）；
        # 远程服务仍然尊重 HTTP_PROXY/HTTPS_PROXY 等环境变量。
        trust_env = not _is_loopback(self.base_url)
        try:
            with httpx.Client(timeout=self.timeout, trust_env=trust_env) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"模型服务请求超时（{self.timeout} 秒），任务已保存检查点，可稍后 lit resume。"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"无法连接模型服务（{type(exc).__name__}）；请检查 provider.base_url 与网络。"
            ) from exc

        if response.status_code >= 400:
            # 只回传状态码，不回传响应体（可能包含密钥或内部信息）
            raise ProviderError(f"模型服务返回 HTTP {response.status_code}（{self.model}）")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("模型服务返回的内容不是 JSON。") from exc
        if not isinstance(data, dict):
            raise ProviderError("模型服务返回的 JSON 不是对象。")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("模型服务返回缺少 choices。")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict) or "content" not in message:
            raise ProviderError("模型服务返回缺少 message.content。")
        content = message.get("content")
        if content is None:
            return ""
        if not isinstance(content, str):
            raise ProviderError("模型服务返回的 message.content 不是字符串。")
        return content