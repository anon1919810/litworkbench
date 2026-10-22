"""本地会话、CSRF 与同源校验。

服务只监听 127.0.0.1，但仍按「本机单用户服务」的方式收紧：

* ``GET /api/v1/workspace`` 建立 HttpOnly + SameSite=Strict 会话 cookie，
  同时在响应体里返回一次 CSRF 令牌（前端只放内存，不写 localStorage）；
* 其他 API 需要有效会话 cookie；
* 写请求必须带 ``X-CSRF-Token``，且 ``Origin`` 必须是本服务自己的来源。

会话保存在服务进程内存里：服务重启后旧 cookie 失效，前端重新
``GET /workspace`` 即可，任务检查点不受影响。
"""

from __future__ import annotations

import hmac
import secrets
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Iterable

from litworkbench.web.errors import error_response

SESSION_COOKIE = "litworkbench_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 32

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 不需要会话的接口（公开健康检查）。
PUBLIC_PATHS = frozenset({"/api/v1/health", "/health"})

#: 建立会话的接口本身：第一次访问还没有 cookie，必须放行。
SESSION_BOOTSTRAP = "/api/v1/workspace"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def trusted_local_host(host: str, server_port: int | None = None) -> bool:
    try:
        parsed = urlsplit("//" + host)
        port = parsed.port or 80
        return bool(parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment
                    and (server_port is None or port == server_port))
    except ValueError:
        return False


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str
    created_at: float


class SessionManager:
    """内存会话表；不写磁盘、不写日志。"""

    def __init__(self, *, ttl_seconds: float = SESSION_TTL_SECONDS,
                 max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds
        self._max = max_sessions

    def issue(self) -> Session:
        now = time.time()
        for token, session in list(self._sessions.items()):
            if now - session.created_at > self._ttl:
                self._sessions.pop(token, None)
        if len(self._sessions) >= self._max:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].created_at)
            self._sessions.pop(oldest, None)
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now)
        self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if time.time() - session.created_at > self._ttl:
            self._sessions.pop(token, None)
            return None
        return session

    def check_csrf(self, session: Session, supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(session.csrf_token, supplied or "")


def _origin_allowed(request, allowed: Iterable[str]) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # 非浏览器客户端（例如本地脚本）没有 Origin
    return origin in set(allowed)


def check_request(request, sessions: SessionManager, *,
                  allowed_origins: Iterable[str]):
    """中间件逻辑：返回错误响应，或 ``None`` 表示放行。"""
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_PATHS:
        return None
    method = request.method.upper()
    bootstrapping = path == SESSION_BOOTSTRAP and method in SAFE_METHODS
    session = sessions.get(request.cookies.get(SESSION_COOKIE))
    if session is None and not bootstrapping:
        return error_response(
            401, "SESSION_REQUIRED",
            "缺少本地会话：请先访问 /api/v1/workspace 建立会话。",
        )
    if method in WRITE_METHODS:
        if session is None or not sessions.check_csrf(session, request.headers.get(CSRF_HEADER)):
            return error_response(
                403, "CSRF_INVALID", "写请求缺少或携带了错误的 X-CSRF-Token。",
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return error_response(403, "ORIGIN_REJECTED", "拒绝跨站写请求。")
        if not _origin_allowed(request, allowed_origins):
            return error_response(
                403, "ORIGIN_REJECTED",
                f"请求来源 {request.headers.get('origin')} 不是本服务，拒绝写入。",
            )
    return None


def set_session_cookie(response, session: Session) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=int(SESSION_TTL_SECONDS),
    )
