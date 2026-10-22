"""任务 3：OpenAI 兼容聊天服务（用本地假服务验证，不接触真实供应商）。"""

from __future__ import annotations

import pytest
from fake_provider import FAKE_KEY, FakeChatServer, completion

from litworkbench.errors import ProviderError, ProviderTimeoutError
from litworkbench.provider import ChatProvider


@pytest.fixture
def server():
    with FakeChatServer(script=['{"tool":"finish","args":{}}']) as fake:
        yield fake


def provider_config(server, **overrides):
    config = {
        "base_url": server.base_url if server else "",
        "model": "fake-model",
        "api_key_env": "LITWORKBENCH_TEST_KEY",
        "timeout": 5.0,
        "temperature": 0.0,
        "max_output_tokens": 256,
    }
    config.update(overrides)
    return config


@pytest.fixture(autouse=True)
def fake_key_env(monkeypatch):
    monkeypatch.setenv("LITWORKBENCH_TEST_KEY", FAKE_KEY)


def test_complete_sends_model_and_key(server):
    provider = ChatProvider(provider_config(server))
    answer = provider.complete([
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "问题"},
    ])
    assert answer == '{"tool":"finish","args":{}}'
    payload = server.requests[0]
    assert payload["model"] == "fake-model"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["content"] == "问题"
    assert server.headers[0]["authorization"] == f"Bearer {FAKE_KEY}"
    assert server.headers[0]["content-type"].startswith("application/json")


def test_missing_api_key_fails_without_network(server, monkeypatch):
    monkeypatch.delenv("LITWORKBENCH_TEST_KEY", raising=False)
    provider = ChatProvider(provider_config(server))
    with pytest.raises(ProviderError) as exc:
        provider.complete([{"role": "user", "content": "问题"}])
    assert "LITWORKBENCH_TEST_KEY" in str(exc.value)
    assert server.hits == 0  # 没有发出任何请求


def test_missing_base_url_fails_clearly(monkeypatch):
    provider = ChatProvider({"api_key_env": "LITWORKBENCH_TEST_KEY", "model": "m"})
    with pytest.raises(ProviderError):
        provider.complete([{"role": "user", "content": "问题"}])


def test_timeout_is_sanitized():
    with FakeChatServer(script=[{"content": "x", "delay": 2.0}]) as slow:
        provider = ChatProvider(provider_config(slow, timeout=0.3))
        with pytest.raises(ProviderTimeoutError) as exc:
            provider.complete([{"role": "user", "content": "问题"}])
        assert FAKE_KEY not in str(exc.value)
        assert "超时" in str(exc.value)


def test_http_error_does_not_leak_body(server):
    server.script = [{"status": 500, "body": {"error": f"token {FAKE_KEY} rejected"}}]
    provider = ChatProvider(provider_config(server))
    with pytest.raises(ProviderError) as exc:
        provider.complete([{"role": "user", "content": "问题"}])
    assert "500" in str(exc.value)
    assert FAKE_KEY not in str(exc.value)


def test_non_json_response_is_reported(server):
    server.script = [{"status": 200, "body": {"choices": "nope"}}]
    provider = ChatProvider(provider_config(server))
    with pytest.raises(ProviderError):
        provider.complete([{"role": "user", "content": "问题"}])


def test_connection_refused_is_reported(monkeypatch):
    provider = ChatProvider(provider_config(None, base_url="http://127.0.0.1:9/v1"))
    with pytest.raises(ProviderError) as exc:
        provider.complete([{"role": "user", "content": "问题"}])
    assert "连接" in str(exc.value) or "Connect" in str(exc.value)


def test_describe_never_contains_key(server):
    provider = ChatProvider(provider_config(server))
    described = provider.describe()
    assert described["base_url"] == server.base_url
    assert described["model"] == "fake-model"
    assert "api_key" not in described
    assert FAKE_KEY not in str(described)


def test_response_without_content_is_error(server):
    server.script = [{"body": {"choices": [{"index": 0, "message": {}}]}}]
    provider = ChatProvider(provider_config(server))
    with pytest.raises(ProviderError):
        provider.complete([{"role": "user", "content": "问题"}])


def test_empty_content_is_returned_as_empty_string(server):
    server.script = [completion("")]
    provider = ChatProvider(provider_config(server))
    assert provider.complete([{"role": "user", "content": "问题"}]) == ""