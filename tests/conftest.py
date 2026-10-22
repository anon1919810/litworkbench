"""共享测试夹具。

所有文档都是**合成测试数据**，由 litworkbench.demo 生成器产生，
不是真实县志或任何真实文献内容。

网页服务夹具也集中放在这里，而不是放在 ``tests/web/conftest.py``：
pytest 9.1.x 在显式文件列表里把 ``tests/`` 顶层模块与 ``tests/web/`` 混排收集时
（例如 ``tests/web/a.py tests/b.py tests/web/c.py``），会为 ``tests/web`` 目录
重复创建 Package 节点；注册在第一个 Package 节点上的夹具，因节点身份不匹配而对
后收集的 ``tests/web`` 用例不可见，表现为 ``fixture 'session' not found``。
``tests/web/__init__.py`` 存在时 ``tests/web`` 也会被重复实例化，因此改写
Package/Dir 标记无法修复；把夹具挂到始终共享、身份稳定的 ``tests`` 包节点上即可。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from litworkbench import demo
from litworkbench.jobs import JobStore
from litworkbench.store import Workspace
from litworkbench.web.app import create_app
from litworkbench.worker import Worker


@pytest.fixture
def workspace(tmp_path):
    """一个位于中文 + 空格路径下的工作区。"""
    root = tmp_path / "工作 空间" / "文献库"
    ws = Workspace(root)
    ws.initialize()
    return ws


@pytest.fixture
def fixtures(tmp_path):
    """生成一组合成 PDF/DOCX 夹具，返回各文件路径。"""
    return demo.make_fixtures(tmp_path / "合成 夹具")


@pytest.fixture
def web_workspace(tmp_path):
    """一个中文 + 空格路径下的工作区。"""
    root = tmp_path / "文献 库" / "个人 图书馆"
    ws = Workspace(root)
    ws.initialize()
    return ws


@pytest.fixture
def web_fixtures(tmp_path):
    return demo.make_fixtures(tmp_path / "合成 资料")


@pytest.fixture
def app(web_workspace):
    return create_app(web_workspace.root, start_worker=False)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


@pytest.fixture
def session(client):
    """先 ``GET /workspace`` 建立会话，返回带 CSRF 头的请求方法。"""
    response = client.get("/api/v1/workspace")
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]

    class Api:
        def __init__(self) -> None:
            self.csrf = token
            self.cookies = response.cookies
            self.client = client

        def write_headers(self, origin: str | None = None) -> dict[str, str]:
            headers = {"X-CSRF-Token": self.csrf}
            if origin:
                headers["Origin"] = origin
            return headers

        def request(self, method: str, url: str, **kwargs):
            return client.request(method, url, **kwargs)

        def get(self, url: str, **kwargs):
            return client.get(url, **kwargs)

        def post(self, url: str, **kwargs):
            headers = dict(self.write_headers(kwargs.pop("origin", None)))
            headers.update(kwargs.pop("headers", {}) or {})
            return client.post(url, headers=headers, **kwargs)

        def patch(self, url: str, **kwargs):
            headers = dict(self.write_headers(kwargs.pop("origin", None)))
            headers.update(kwargs.pop("headers", {}) or {})
            return client.patch(url, headers=headers, **kwargs)

        def upload(self, files: list[tuple[str, bytes]], **kwargs):
            headers = dict(self.write_headers(kwargs.pop("origin", None)))
            headers.update(kwargs.pop("headers", {}) or {})
            return client.post("/api/v1/imports", files=files, headers=headers, **kwargs)

    return Api()


@pytest.fixture
def jobs(web_workspace):
    return JobStore(web_workspace)


@pytest.fixture
def run_worker(web_workspace):
    """在当前进程里驱动真实后台执行者（不经过 HTTP）。"""

    def _run(max_jobs: int = 10) -> list[dict]:
        from litworkbench.ingest import _ocr_page
        worker = Worker(web_workspace, ocr_page=_ocr_page)
        processed: list[dict] = []
        for _ in range(max_jobs):
            job = worker.tick()
            if job is None:
                break
            processed.append(job)
        return processed

    return _run


def copy_fixture(source: Path, dest_dir: Path, name: str | None = None) -> Path:
    """复制一份夹具（可选改名），用于构造上传/中文文件名场景。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (name or source.name)
    shutil.copy2(source, target)
    return target
