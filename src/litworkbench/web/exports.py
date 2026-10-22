"""资料导出接口：预览 → 生成 → 按 ID 下载。

流程刻意分两步：

1. ``POST /exports/preview`` 只算数量、核验状态分布与内容摘要，**不写文件**；
2. ``POST /exports`` 必须带回同一个 ``snapshot_token``。生成时重新读一次快照，
   摘要不一致（有人校订原文、改核验状态或改书目）就返回 409，
   要求重新预览，绝不输出基于旧判断的文件。

下载只按记录主键解析 ``artifacts/exports`` 里的文件，没有任意路径参数。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from litworkbench import evidence_exports as core
from litworkbench.errors import EditConflictError, ValidationError
from litworkbench.store import Workspace
from litworkbench.web.schemas import ExportCreate, ExportFilters, ExportPreview

router = APIRouter()


def workspace_of(request: Request) -> Workspace:
    return request.app.state.workspace


def _scope_from(filters: ExportFilters, evidence_ids: list[str] | None) -> core.Scope:
    return core.normalize_scope(
        doc_id=filters.doc_id,
        category=filters.category,
        review_status=filters.review_status,
        collection_id=filters.collection_id,
        evidence_ids=evidence_ids,
    )


@router.post("/exports/preview")
def preview_export(request: Request, body: ExportPreview) -> dict[str, Any]:
    scope = _scope_from(body.filters, body.evidence_ids)
    snapshot = core.build_snapshot(workspace_of(request), body.format, scope)
    return {
        "count": snapshot.count,
        "review_counts": snapshot.review_counts,
        "snapshot_token": snapshot.token,
        "filename": snapshot.filename,
        "format": snapshot.format,
        "scope": snapshot.scope.as_dict(),
        "scope_description": snapshot.scope_description,
        "review_policy": snapshot.scope.review_policy,
        "material_note": core.MATERIAL_NOTE,
        "review_note": core.REVIEW_NOTE,
    }


@router.post("/exports", status_code=201)
def create_export(request: Request, body: ExportCreate) -> dict[str, Any]:
    workspace = workspace_of(request)
    scope = _scope_from(body.filters, body.evidence_ids)
    snapshot = core.build_snapshot(workspace, body.format, scope)
    if not snapshot.count:
        raise ValidationError(
            "当前范围没有可导出的摘录；请调整筛选或显式选择至少一条。"
        )
    if not body.snapshot_token:
        raise ValidationError("缺少 snapshot_token：请先预览再生成。")
    if body.snapshot_token != snapshot.token:
        raise EditConflictError(
            "预览之后内容发生了变化（校订、审核或书目改动），已拒绝生成旧结果；"
            "请重新预览后再生成。"
        )
    data = core.render(snapshot)
    record = core.store_export(workspace, snapshot, data)
    return core.export_view(record)


@router.get("/exports/{export_id}")
def read_export(export_id: str, request: Request) -> dict[str, Any]:
    return core.export_view(core.get_export(workspace_of(request), export_id))


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, request: Request) -> FileResponse:
    workspace = workspace_of(request)
    record = core.get_export(workspace, export_id)
    path = core.export_path(workspace, record)
    return FileResponse(
        path,
        media_type=core.MEDIA_TYPES.get(record["format"], "application/octet-stream"),
        filename=record["filename"],
        headers={"Cache-Control": "no-store", "X-Export-Sha256": record["sha256"]},
    )