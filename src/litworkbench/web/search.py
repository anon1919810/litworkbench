"""检索与覆盖接口。

结果、总数、覆盖与索引代次来自**同一个读事务**；分页代次不一致时返回
``409 SEARCH_INDEX_CHANGED``，前端保留条件并重查第一页。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from litworkbench import search_index
from litworkbench.store import Workspace
from litworkbench.web.errors import error_response
from litworkbench.web.pagination import normalize_page
from litworkbench.web.schemas import SearchRequest

router = APIRouter()


def workspace_of(request: Request) -> Workspace:
    return request.app.state.workspace


@router.post("/search")
def search(request: Request, body: SearchRequest):
    workspace = workspace_of(request)
    page, page_size = normalize_page(body.page, body.page_size)
    try:
        return search_index.search_indexed(
            workspace,
            body.query,
            mode=body.mode,
            doc_ids=body.doc_ids,
            page=page,
            page_size=page_size,
            expected_index_revision=body.expected_index_revision,
        )
    except search_index.IndexRevisionChanged as exc:
        return error_response(
            409, "SEARCH_INDEX_CHANGED",
            "资料已更新（文字、页码或书目发生变化），请用相同条件重新查询第一页。",
            exc.details,
        )


@router.get("/coverage")
def coverage(request: Request) -> dict[str, Any]:
    """覆盖对象：已索引文字页 + 人工确认空白页才算覆盖。"""
    workspace = workspace_of(request)
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        data = search_index.coverage(conn)
        revision = search_index.current_revision(conn)
        pending = search_index.pending_count(conn)
        conn.commit()
    data["index_revision"] = revision
    data["pending_index_units"] = pending
    return data