"""页面、块、笔记图片与页面导出的网页适配层。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse

from litworkbench import note_assets, note_exports, notes
from litworkbench.errors import ValidationError
from litworkbench.store import Workspace
from litworkbench.web.note_schemas import (
    PageBlocks,
    PageCreate,
    PageExportRequest,
    PagePatch,
    PageRestore,
)

router = APIRouter()

DEFAULT_PAGE_SIZE = notes.DEFAULT_PAGE_SIZE
MAX_PAGE_SIZE = notes.MAX_PAGE_SIZE


def workspace_of(request: Request) -> Workspace:
    return request.app.state.workspace


# ------------------------------------------------------------------ 页面


@router.get("/pages")
def list_pages(request: Request, page: int = Query(1), page_size: int = Query(30),
               q: str = Query(""), query: str | None = Query(None),
               include_archived: bool = Query(False)) -> dict[str, Any]:
    return notes.list_pages(
        workspace_of(request),
        query=query if query is not None else q,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )


@router.post("/pages", status_code=201)
def create_page(request: Request, body: PageCreate) -> dict[str, Any]:
    return notes.create_page(
        workspace_of(request),
        title=body.title,
        parent_id=body.parent_id,
        body=body.body,
        properties=body.properties,
        request_id=body.request_id,
        actor="owner",
        origin="web",
    )


@router.get("/pages/{page_id}")
def get_page(page_id: str, request: Request) -> dict[str, Any]:
    return notes.get_page(workspace_of(request), page_id)


@router.patch("/pages/{page_id}")
def patch_page(page_id: str, request: Request, body: PagePatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    expected_revision = changes.pop("expected_revision")
    request_id = changes.pop("request_id")
    return notes.update_page(
        workspace_of(request),
        page_id,
        expected_revision=expected_revision,
        request_id=request_id,
        changes=changes,
        actor="owner",
        origin="web",
    )


@router.get("/pages/{page_id}/history")
def page_history(page_id: str, request: Request, page: int = Query(1),
                 page_size: int = Query(DEFAULT_PAGE_SIZE)) -> dict[str, Any]:
    return notes.get_page_history(workspace_of(request), page_id,
                                  page=page, page_size=page_size)


@router.get("/pages/{page_id}/backlinks")
def page_backlinks(page_id: str, request: Request, page: int = Query(1),
                   page_size: int = Query(DEFAULT_PAGE_SIZE)) -> dict[str, Any]:
    return notes.get_backlinks(workspace_of(request), page_id,
                               page=page, page_size=page_size)


@router.post("/pages/{page_id}/restore")
def restore_page(page_id: str, request: Request, body: PageRestore) -> dict[str, Any]:
    return notes.restore_page(
        workspace_of(request),
        page_id,
        expected_revision=body.expected_revision,
        request_id=body.request_id,
        revision=body.revision,
        actor="owner",
        origin="web",
    )


@router.post("/pages/{page_id}/blocks")
def edit_page_blocks(page_id: str, request: Request,
                     body: PageBlocks) -> dict[str, Any]:
    return notes.edit_page_blocks(
        workspace_of(request),
        page_id,
        expected_revision=body.expected_revision,
        request_id=body.request_id,
        operations=body.operations,
        actor="owner",
        origin="web",
    )


# ------------------------------------------------------------------ 页面导出


@router.post("/pages/{page_id}/exports", status_code=201)
def create_page_export(page_id: str, request: Request,
                       body: PageExportRequest) -> dict[str, Any]:
    record = note_exports.create_page_export(workspace_of(request), page_id, body.format)
    return note_exports.export_view(record)


@router.get("/page-exports/{export_id}")
def read_page_export(export_id: str, request: Request) -> dict[str, Any]:
    return note_exports.export_view(
        note_exports.get_page_export(workspace_of(request), export_id)
    )


@router.get("/page-exports/{export_id}/download")
def download_page_export(export_id: str, request: Request) -> FileResponse:
    workspace = workspace_of(request)
    record = note_exports.get_page_export(workspace, export_id)
    path = note_exports.page_export_path(workspace, record)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=record["filename"],
        headers={"Cache-Control": "no-store", "X-Export-Sha256": record["sha256"]},
    )


# ------------------------------------------------------------------ 笔记图片


@router.post("/note-assets", status_code=201)
async def upload_note_asset(request: Request,
                            file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        data = await file.read(note_assets.MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    if len(data) > note_assets.MAX_IMAGE_BYTES:
        raise note_assets.NoteAssetTooLargeError("单张图片不能超过 20 MiB。")
    record = note_assets.store_asset(
        workspace_of(request),
        filename=file.filename or "图片",
        data=data,
    )
    return note_assets.asset_view(record)


@router.get("/note-assets/{asset_id}")
def read_note_asset(asset_id: str, request: Request) -> FileResponse:
    workspace = workspace_of(request)
    record = note_assets.get_asset(workspace, asset_id)
    path = note_assets.asset_path(workspace, record)
    return FileResponse(
        path,
        media_type=record["media_type"],
        headers={"Cache-Control": "private, no-store",
                 "Content-Disposition": "inline"},
    )


__all__ = ["router"]
