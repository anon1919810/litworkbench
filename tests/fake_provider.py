"""测试专用的本地假 OpenAI 兼容 HTTP 服务。

只用于测试，**不进入生产代码路径**：它只是在本机随机端口上回放脚本化的
JSON 回复，用来验证多轮动作协议、错误处理与恢复流程，不代表任何真实供应商。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FAKE_KEY = "sk-test-FAKE-KEY-DO-NOT-USE"  # noqa: S105 - 仅测试用的假密钥


def completion(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeChatServer:
    """回放脚本化回复的本地服务。"""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.errors: list[str] = []
        self._index = 0
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音
                pass

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {"__raw__": raw.decode("utf-8", "replace")}
                server_self.requests.append(payload)
                server_self.headers.append({k.lower(): v for k, v in self.headers.items()})

                item = None
                if callable(server_self.script):
                    item = server_self.script(server_self._index, payload)
                elif server_self.script:
                    item = server_self.script[min(server_self._index,
                                                 len(server_self.script) - 1)]
                server_self._index += 1

                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, str):
                    item = completion(item)
                item = item or completion("{}")
                delay = float(item.pop("delay", 0) or 0)
                status = int(item.pop("status", 200) or 200)
                body = item.pop("body", None)
                if delay:
                    import time

                    time.sleep(delay)
                data = json.dumps(body if body is not None else item).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ------------------------------------------------------------ 生命周期

    def __enter__(self) -> "FakeChatServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -------------------------------------------------------------- 属性

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def hits(self) -> int:
        return len(self.requests)

    def wait_for(self, count: int, timeout: float = 5.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.hits >= count:
                return True
            time.sleep(0.02)
        return False