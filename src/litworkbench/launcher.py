"""本地网页启动器：选端口、启动服务、打开浏览器、关闭时清理。

* 只监听 ``127.0.0.1``；
* 端口被占用时自动往后找，最多尝试 ``attempts`` 个；
* 用 Uvicorn 的 lifespan 管理后台识别子进程，退出时优雅停止；
* 静态资源解析顺序：源码目录的 ``web-client/dist`` -> 包内 ``web/static``。
"""

from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from litworkbench.errors import LitError

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
MAX_PORT_ATTEMPTS = 20


def find_free_port(host: str = DEFAULT_HOST, start: int = DEFAULT_PORT,
                   attempts: int = MAX_PORT_ATTEMPTS) -> int:
    """从 ``start`` 起找一个没人监听的端口。"""
    for offset in range(attempts):
        candidate = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise LitError(f"{start}-{start + attempts - 1} 端口都被占用，请用 --port 指定其他端口。")


def resolve_static_dir(workspace_root: Path | str | None = None) -> tuple[Path | None, list[Path]]:
    """返回 ``(静态目录, 依次查找过的目录)``。

    顺序固定为：源码仓库里的 ``web-client/dist``、随包分发的 ``web/static``。
    找不到时返回 ``None``，调用方给出明确的中文错误。
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent / "web-client" / "dist",
        here / "web" / "static",
    ]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").exists():
            return candidate, candidates
    return None, candidates


def open_browser(url: str, *, delay: float = 1.2) -> bool:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - 没有浏览器的环境
            pass

    threading.Thread(target=_open, daemon=True, name="litworkbench-browser").start()
    return True


def launch(workspace_root: Path | str, *, port: int = DEFAULT_PORT,
           open_browser_flag: bool = True, host: str = DEFAULT_HOST,
           log_level: str = "info") -> int:
    """启动本地网页服务（阻塞）。返回进程退出码。"""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise LitError(
            "未安装网页依赖。请执行：uv pip install --python <python> -e '.[web]'"
        ) from exc

    from litworkbench.store import Workspace
    from litworkbench.web.app import create_app

    root = Path(workspace_root).expanduser()
    workspace = Workspace(root)
    if not workspace.exists():
        workspace.initialize()
    chosen = find_free_port(host, int(port) or DEFAULT_PORT)
    app = create_app(workspace.root, start_worker=True, server_port=chosen)
    url = f"http://{host}:{chosen}/"
    if open_browser_flag:
        open_browser(url)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    uvicorn.run(app, host=host, port=chosen, log_level=log_level, access_log=False)
    return 0
