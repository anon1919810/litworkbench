"""FastAPI 应用装配：同源静态资源 + ``/api/v1`` 契约 + 后台识别子进程。

``create_app(workspace_root, start_worker=True)`` 是唯一的组装入口：

* 打开（必要时初始化）工作区并执行增量迁移；
* 注册中文结构化错误、会话/CSRF/同源校验中间件；
* lifespan 里启动**一个**后台识别子进程，退出时优雅停止；
* 静态资源回退最后注册，**不会吞掉 API 路由或 API 的 404**。
"""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from litworkbench import ocr as ocr_mod
from litworkbench import search_index
from litworkbench.jobs import JobStore
from litworkbench.ocr_candidates import CandidateStore
from litworkbench.store import Workspace
from litworkbench.runtime import RuntimeSettings
from litworkbench.web import collections as collections_mod
from litworkbench.web import documents as documents_mod
from litworkbench.web import evidence as evidence_mod
from litworkbench.web import exports as exports_mod
from litworkbench.web import jobs as jobs_mod
from litworkbench.web import notes as notes_mod
from litworkbench.web import ocr_candidates as ocr_candidates_mod
from litworkbench.web import search as search_mod
from litworkbench.web.errors import error_response, register_error_handlers
from litworkbench.web.security import SESSION_COOKIE, SessionManager, check_request, set_session_cookie, trusted_local_host
from litworkbench.web.statics import register_statics

API_PREFIX = "/api/v1"


class WorkerProcess:
    """后台识别子进程句柄：用停止标记实现跨平台的优雅退出。"""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root)
        self.marker = self.workspace_root / ".litworkbench" / "worker.stop"
        self.process: subprocess.Popen[bytes] | None = None
        self.create_time: float | None = None

    def start(self) -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.unlink(missing_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "litworkbench.worker", "--workspace",
             str(self.workspace_root)],
            stdout=subprocess.DEVNULL,
            stderr=None,
            stdin=subprocess.DEVNULL,
        )
        from litworkbench.ocr_process import _load_psutil, _get_create_time
        module = _load_psutil()
        self.create_time = _get_create_time(module, self.process.pid) if module else None

    def stop(self, timeout: float = 15.0) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        from litworkbench.ocr_process import _load_psutil, _get_create_time, _terminate_tree, _probe_process
        module = _load_psutil()
        identity = self.create_time or (_get_create_time(module, process.pid) if module else None)
        descendants = {}

        def remember_tree():
            if module and _probe_process(module, process.pid, identity) == "alive":
                try:
                    for child in module.Process(process.pid).children(recursive=True):
                        descendants[child.pid] = child.create_time()
                except Exception:
                    pass  # The cleanup helper conservatively retries live-tree inspection.

        remember_tree()
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("stop", encoding="utf-8")
        deadline = time.time() + timeout
        while time.time() < deadline and process.poll() is None:
            remember_tree()
            time.sleep(0.1)
        if process.poll() is None or descendants:
            reaped = _terminate_tree(process, module, expected_create_time=identity, descendants=descendants)
            if not reaped:
                raise RuntimeError("无法确认后台识别进程树已完全回收；请保留闸门并检查后台进程")
        self.marker.unlink(missing_ok=True)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def coverage_payload(workspace: Workspace) -> dict[str, Any]:
    """覆盖 + 代次 + 待索引数：同一个读事务里取，避免拼出不一致的结果。"""
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        data = search_index.coverage(conn)
        data["index_revision"] = search_index.current_revision(conn)
        data["pending_index_units"] = search_index.pending_count(conn)
        conn.commit()
    return data


def create_app(workspace_root: Path | str, *, start_worker: bool = True,
               static_dir: Path | str | None = None,
               server_port: int | None = None,
               settings: RuntimeSettings | None = None) -> FastAPI:
    """组装本地网页服务。``start_worker=False`` 时不拉起后台识别进程（测试用）。"""
    settings = settings or RuntimeSettings.from_env()
    root = Path(workspace_root).expanduser()
    workspace = Workspace(root)
    if settings.mode == 'cloud' and not workspace.exists():
        raise ValueError('Cloud workspace must be explicitly initialized before starting')
    if not workspace.exists():
        workspace.initialize()
    else:
        from litworkbench import migrations

        migrations.ensure_schema(workspace)
    store = JobStore(workspace)
    sessions = SessionManager()
    worker = WorkerProcess(workspace.root) if start_worker and (settings.mode == 'local' or settings.worker_enabled) else None
    mcp = None
    mcp_app = None
    if settings.mode == 'cloud':
        from litworkbench.cloud_auth import CloudAuth
        from litworkbench.mcp_server import build_mcp
        cloud_auth = CloudAuth(workspace, settings)
        mcp, mcp_app = build_mcp(workspace, cloud_auth, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if worker is not None:
            worker.start()
            store.recover_stale()  # 服务重启后收拢上次遗留的任务
            try:
                store.enqueue_index()  # 脏单元在这里重建索引
            except Exception:  # pragma: no cover - 排队失败不应阻断服务
                pass
        try:
            async with AsyncExitStack() as stack:
                if mcp is not None:
                    await stack.enter_async_context(mcp.session_manager.run())
                yield
        finally:
            if worker is not None:
                worker.stop()

    app = FastAPI(
        title="文献工作台",
        version="0.1.0",
        description="本地单用户文献工作台：导入、后台识别、中文检索、原页核对与摘录。",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.workspace = workspace
    app.state.jobs = store
    app.state.candidates = CandidateStore(workspace)
    app.state.sessions = sessions
    app.state.worker = worker
    app.state.settings = settings
    from litworkbench.web import auth as auth_mod
    if settings.mode == 'cloud':
        app.state.cloud_auth = cloud_auth
        app.state.oidc = auth_mod.configure_oidc(settings)

    register_error_handlers(app)

    @app.middleware("http")
    async def session_guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if settings.mode == 'cloud':
            from litworkbench.web.cloud_security import check_cloud_request
            failure = check_cloud_request(request, app.state.cloud_auth, settings)
            if failure is not None:
                return failure
            response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith(('/api/', '/auth/', '/mcp')) else response.headers.get('Cache-Control', 'no-cache')
            response.headers['Referrer-Policy'] = 'same-origin'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
        if not trusted_local_host(host, server_port):
            return error_response(403, "HOST_REJECTED", "只接受本机地址及当前服务端口。")
        allowed = {f"{request.url.scheme}://{host}"}
        failure = check_request(request, sessions, allowed_origins=allowed)
        if failure is not None:
            return failure
        return await call_next(request)

    @app.get("/health", include_in_schema=False)
    @app.get(f"{API_PREFIX}/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/workspace")
    def workspace_info(request: Request) -> JSONResponse:
        """建立本地会话并返回工作区概况；csrf_token 只在响应体里给一次。"""
        if settings.mode == 'cloud':
            from litworkbench.cloud_auth import SESSION_COOKIE as CLOUD_COOKIE
            session = app.state.cloud_auth.get_session(request.cookies.get(CLOUD_COOKIE))
        else:
            session = sessions.get(request.cookies.get(SESSION_COOKIE)) or sessions.issue()
        payload: dict[str, Any] = {
            "name": workspace.root.name,
            "root": str(workspace.root),
            "csrf_token": session.csrf_token,
            "ocr": ocr_mod.availability(),
            "coverage": coverage_payload(workspace),
        }
        response = JSONResponse(payload)
        if settings.mode == 'cloud':
            payload.pop('root')
            payload['mode'] = 'cloud'
            payload['worker_enabled'] = worker is not None
            response = JSONResponse(payload)
        else:
            set_session_cookie(response, session)
        return response

    app.include_router(documents_mod.router, prefix=API_PREFIX)
    app.include_router(jobs_mod.router, prefix=API_PREFIX)
    app.include_router(ocr_candidates_mod.router, prefix=API_PREFIX)
    app.include_router(search_mod.router, prefix=API_PREFIX)
    app.include_router(evidence_mod.router, prefix=API_PREFIX)
    app.include_router(collections_mod.router, prefix=API_PREFIX)
    app.include_router(exports_mod.router, prefix=API_PREFIX)
    app.include_router(notes_mod.router, prefix=API_PREFIX)
    app.include_router(auth_mod.router)
    if mcp_app is not None:
        from starlette.routing import Route
        app.router.routes.append(Route('/mcp', endpoint=mcp_app))
    # 静态资源与 SPA 回退必须最后注册：前面的 API 路由优先匹配，
    # 未命中的 /api/... 也会由回退返回 JSON 404 而不是 index.html。
    app.state.static_dir = register_statics(
        app, Path(static_dir) if static_dir else None
    )
    return app


__all__ = ["create_app", "WorkerProcess", "coverage_payload"]
