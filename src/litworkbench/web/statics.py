"""同源静态资源与单页应用回退。

解析顺序固定：源码目录的 ``web-client/dist``，然后随包分发的 ``web/static``。
两条硬规则：

* **API 的 404 不能被回退吞掉**：``/api/...`` 一律返回 JSON 404；
* **缺编译产物要明确报错**：给出中文说明与查找过的目录，不返回空白页。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from litworkbench.web.errors import error_body

#: 这些前缀不属于前端单页应用，必须由 API 自己回答。
API_PREFIXES = ("api/", "docs", "redoc", "openapi.json")


def resolve_static_dir() -> tuple[Path | None, list[Path]]:
    from litworkbench.launcher import resolve_static_dir as resolve

    return resolve()


def missing_build_page(candidates: list[Path]) -> HTMLResponse:
    checked = "\n".join(f"<li>{path}</li>" for path in candidates)
    return HTMLResponse(
        status_code=503,
        content=(
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>文献工作台：缺少前端编译产物</title></head>"
            "<body style=\"font-family:Microsoft YaHei,Segoe UI,sans-serif;max-width:720px;"
            "margin:48px auto;line-height:1.8\">"
            "<h1>缺少前端编译产物</h1>"
            "<p>接口服务已经正常启动，但找不到网页前端文件。请在源码目录执行：</p>"
            "<pre>npm --prefix web-client install\nnpm --prefix web-client run build</pre>"
            f"<p>已按顺序查找：</p><ul>{checked}</ul>"
            "<p>日常使用不需要 Node；这一步只在开发或重新构建时需要。</p>"
            "</body></html>"
        ),
    )


def register_statics(app: FastAPI, static_dir: Path | None = None) -> Path | None:
    """挂载静态资源与 SPA 回退；返回实际使用的目录（可能是 ``None``）。"""
    if static_dir is not None:
        candidate = Path(static_dir)
        candidates = [candidate]
        resolved = candidate if (candidate / "index.html").is_file() else None
    else:
        resolved, candidates = resolve_static_dir()

    if resolved is not None and (resolved / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(resolved / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request) -> Response:
        if any(full_path == prefix or full_path.startswith(prefix) for prefix in API_PREFIXES):
            return JSONResponse(
                status_code=404,
                content=error_body("NOT_FOUND", f"接口不存在：/{full_path}"),
            )
        if resolved is None:
            return missing_build_page(candidates)
        if full_path:
            candidate = (resolved / full_path).resolve()
            try:
                candidate.relative_to(resolved.resolve())
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                return FileResponse(candidate)
        index = resolved / "index.html"
        if not index.exists():  # pragma: no cover - 解析时已校验
            return missing_build_page(candidates)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return resolved