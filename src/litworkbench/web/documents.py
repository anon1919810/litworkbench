"""文献、原文单元、上传导入与重新识别。

约定：

* 上传走流式暂存，每文件上限 1GiB；文件名只作为**数据**处理，
  同时去掉 Windows 与 Unix 路径成分，绝不接受服务器路径；
* 内容哈希去重，不依赖文件名；
* HTTP 请求里**不做长时间 OCR**：只暂存 + 建任务，识别在后台进程里；
* 原件下载与页图渲染都按稳定 ID 查找，不接受任意服务器路径。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from litworkbench import search_index
from litworkbench.errors import (
    LitError,
    NotFoundError,
    ValidationError,
)
from litworkbench.evidence import correct_unit, effective_text
from litworkbench.ingest import SUPPORTED_EXTENSIONS
from litworkbench.jobs import JobStore
from litworkbench.render_process import request_render, validate_scale
from litworkbench.store import Workspace, ensure_write_transaction, sha256_text, utcnow
from litworkbench.text import UnitStatus
from litworkbench.web.pagination import normalize_page, offset_of, paginate
from litworkbench.web.schemas import (
    BIBLIOGRAPHY_FIELDS,
    CorrectionRequest,
    DocumentPatch,
    PrintedPageRequest,
    ReprocessRequest,
    validate_ocr_mode,
)

router = APIRouter()

#: 单个上传文件上限 1GiB（可用环境变量 LITWORKBENCH_MAX_UPLOAD_BYTES 调整）。
MAX_UPLOAD_BYTES = int(os.environ.get("LITWORKBENCH_MAX_UPLOAD_BYTES", 1024 ** 3))
CHUNK_SIZE = 1 << 20

_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"


def workspace_of(request: Request) -> Workspace:
    return request.app.state.workspace


def jobs_of(request: Request) -> JobStore:
    return request.app.state.jobs


# ------------------------------------------------------------------ 序列化


def document_item(workspace: Workspace, row: dict[str, Any]) -> dict[str, Any]:
    """契约里的文献条目（page_count 可以是 null）。"""
    with workspace.connect() as conn:
        searchable = search_index.searchable_pages(conn, row["id"])
    return {
        "id": row["id"],
        "title": row["title"],
        "filename": row["filename"],
        "filetype": row["filetype"],
        "page_count": row["page_count"],
        "unit_count": row["unit_count"],
        "status": row["status"],
        "pending_count": row["pending_count"],
        "searchable_pages": searchable,
        "author": row["author"],
        "year": row["year"],
        "edition": row["edition"],
        "volume": row["volume"],
        "sha256": row["sha256"],
        "ocr_mode": row["ocr_mode"],
        "imported_at": row["imported_at"],
        "updated_at": row["updated_at"],
    }


def unit_brief(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": unit["id"],
        "doc_id": unit["doc_id"],
        "seq": unit["seq"],
        "kind": unit["kind"],
        "file_page": unit["file_page"],
        "printed_page": unit["printed_page"],
        "printed_page_status": unit.get("printed_page_status") or "none",
        "status": unit["status"],
        "method": unit["method"],
        "title_path": unit["title_path"],
    }


def unit_detail(workspace: Workspace, unit: dict[str, Any]) -> dict[str, Any]:
    document = workspace.get_document(unit["doc_id"])
    text, version = effective_text(unit)
    ocr = unit.get("ocr_json") or None
    if ocr is not None:
        ocr = {
            "engine": ocr.get("engine"),
            "lines": ocr.get("lines") or [],
            "mean_confidence": ocr.get("mean_confidence"),
            "render_width": ocr.get("render_width"),
            "render_height": ocr.get("render_height"),
        }
    return {
        "unit_id": unit["id"],
        "doc_id": unit["doc_id"],
        "title": document["title"],
        "filename": document["filename"],
        "kind": unit["kind"],
        "seq": unit["seq"],
        "file_page": unit["file_page"],
        "printed_page": unit["printed_page"],
        "printed_page_status": unit.get("printed_page_status") or "none",
        "title_path": unit["title_path"],
        "method": unit["method"],
        "status": unit["status"],
        "notes": unit["notes"],
        "text": text,
        "original_text": unit["original_text"],
        "corrected_text": unit["corrected_text"],
        "version": version,
        "text_sha256": sha256_text(text),
        "content_revision": int(unit.get("content_revision") or 0),
        "ocr": ocr,
        "revisions": [
            {"at": item["at"], "kind": item["kind"], "reason": item["reason"]}
            for item in workspace.revisions_for_unit(unit["id"])
        ],
    }


# ------------------------------------------------------------------ 文献


@router.get("/documents")
def list_documents(request: Request, page: int = Query(1), page_size: int = Query(30),
                   status: str | None = Query(None)) -> dict[str, Any]:
    workspace = workspace_of(request)
    page, page_size = normalize_page(page, page_size)
    where, params = "", []
    if status:
        where = "WHERE status = ?"
        params.append(status)
    with workspace.connect() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM documents {where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM documents {where} ORDER BY imported_at DESC, id "
            f"LIMIT ? OFFSET ?",
            [*params, page_size, offset_of(page, page_size)],
        ).fetchall()
    items = [document_item(workspace, dict(row)) for row in rows]
    return paginate(items, total, page, page_size)


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, request: Request) -> dict[str, Any]:
    workspace = workspace_of(request)
    return document_item(workspace, workspace.get_document(doc_id))


@router.patch("/documents/{doc_id}")
def patch_document(doc_id: str, request: Request, body: DocumentPatch) -> dict[str, Any]:
    """仅可编辑书目字段；其他字段由导入与识别流程维护。"""
    workspace = workspace_of(request)
    workspace.get_document(doc_id)
    submitted = body.model_dump(exclude_unset=True)
    unknown = sorted(set(submitted) - set(BIBLIOGRAPHY_FIELDS))
    if unknown:
        raise ValidationError(
            f"只能编辑书目字段：{'、'.join(BIBLIOGRAPHY_FIELDS)}；收到 {'、'.join(unknown)}"
        )
    changes: dict[str, str] = {}
    for field, value in submitted.items():
        if field not in BIBLIOGRAPHY_FIELDS:
            continue
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"{field} 必须是字符串。")
        changes[field] = "" if value is None else value
    if not changes:
        raise ValidationError("没有可修改的书目字段（title/author/year/edition/volume）。")
    updated = workspace.update_document(doc_id, **changes)
    return document_item(workspace, updated)


@router.get("/documents/{doc_id}/units")
def list_units(doc_id: str, request: Request, page: int = Query(1),
               page_size: int = Query(30), status: str | None = Query(None)) -> dict[str, Any]:
    workspace = workspace_of(request)
    workspace.get_document(doc_id)
    page, page_size = normalize_page(page, page_size)
    where, params = "WHERE doc_id = ?", [doc_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    with workspace.connect() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM units {where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM units {where} ORDER BY seq LIMIT ? OFFSET ?",
            [*params, page_size, offset_of(page, page_size)],
        ).fetchall()
    items = [unit_brief(dict(row)) for row in rows]
    return paginate(items, total, page, page_size)


@router.get("/documents/{doc_id}/source")
def download_source(doc_id: str, request: Request) -> Response:
    workspace = workspace_of(request)
    document = workspace.get_document(doc_id)
    path = workspace.path_of(document)
    if not path.exists():
        raise NotFoundError(f"原件副本缺失：{document['filename']}")
    return FileResponse(
        path,
        media_type="application/pdf" if document["filetype"] == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=document["filename"],
    )


# ------------------------------------------------------------------ 单元


@router.get("/units/{unit_id}")
def get_unit(unit_id: str, request: Request) -> dict[str, Any]:
    workspace = workspace_of(request)
    return unit_detail(workspace, workspace.get_unit(unit_id))


@router.get("/units/{unit_id}/render")
def render_unit(unit_id: str, request: Request,
                scale: float = Query(1.5)) -> Response:
    """在隔离的渲染子进程里出图；PDFium 不会与 OCR 并发调用。"""
    workspace = workspace_of(request)
    unit = workspace.get_unit(unit_id)
    if unit["kind"] != "page":
        raise ValidationError("只有 PDF 页单元可以渲染页图；Word 请使用文档文字视图。")
    document = workspace.get_document(unit["doc_id"])
    if document["filetype"] != "pdf":
        raise ValidationError(f"文献 {document['id']} 不是 PDF，无法渲染页图。")
    chosen = validate_scale(scale)
    data = request_render(workspace.path_of(document), int(unit["file_page"]) - 1, chosen)
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store",
                 "X-Render-Scale": f"{chosen:g}",
                 "X-Unit-Id": unit_id},
    )


@router.patch("/units/{unit_id}/correction")
def patch_correction(unit_id: str, request: Request,
                     body: CorrectionRequest) -> dict[str, Any]:
    """校订：哈希校验与写入在同一个写事务里完成。

    ``BEGIN IMMEDIATE`` 先取写锁，再读当前文字校验 ``expected_hash``，
    然后写入校订、历史与证据复核状态；后台 OCR 或其他校订无法插进中间。
    """
    workspace = workspace_of(request)
    if not body.reason.strip():
        raise ValidationError("校订必须填写原因。")
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        current = workspace.get_unit(unit_id, connection=conn)
        text, _version = effective_text(current)
        if body.expected_hash and sha256_text(text) != body.expected_hash:
            raise _conflict(unit_id, sha256_text(text))
        correct_unit(workspace, unit_id, body.text, body.reason,
                     expected_hash=body.expected_hash, connection=conn)
    try:
        search_index.index_unit(workspace, unit_id)
    except Exception:  # pragma: no cover - 索引失败由后台索引任务补齐
        pass
    return unit_detail(workspace, workspace.get_unit(unit_id))


def _conflict(unit_id: str, current_hash: str):
    from litworkbench.errors import EditConflictError

    return EditConflictError(
        f"单元 {unit_id} 的文字已在你编辑期间被改动（当前版本 {current_hash[:12]}…），"
        "请重新核对后保存。"
    )


@router.patch("/units/{unit_id}/printed-page")
def patch_printed_page(unit_id: str, request: Request,
                       body: PrintedPageRequest) -> dict[str, Any]:
    """印刷页码与文件页序分开保存；候选值不会自动当成已确认。"""
    workspace = workspace_of(request)
    unit = workspace.get_unit(unit_id)
    if not str(body.printed_page).strip():
        raise ValidationError("印刷页码不能为空；如需清除请使用校订流程。")
    with workspace.connect() as conn:
        conn.execute(
            "UPDATE units SET printed_page = ?, printed_page_status = ?, updated_at = ? "
            "WHERE id = ?",
            (str(body.printed_page).strip(), body.status, utcnow(), unit_id),
        )
    return unit_detail(workspace, workspace.get_unit(unit_id))


@router.post("/units/{unit_id}/confirm-blank")
def confirm_blank(unit_id: str, request: Request) -> dict[str, Any]:
    """人工确认空白页：只有无文字、未完成的页可以确认。"""
    workspace = workspace_of(request)
    with workspace.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        unit = workspace.get_unit(unit_id, connection=conn)
        text, _version = effective_text(unit)
        if text.strip():
            raise ValidationError("这一页有文字，不能确认为空白；请先校订为空白或删除多余文字。")
        if unit["status"] == UnitStatus.COMPLETED and not unit["blank_confirmed"]:
            raise ValidationError("这一页已经完成，不需要确认为空白。")
        conn.execute(
            "UPDATE units SET status = ?, blank_confirmed = 1, notes = ?, updated_at = ? "
            "WHERE id = ?",
            (UnitStatus.COMPLETED, "人工确认：空白页", utcnow(), unit_id),
        )
    search_index.index_unit(workspace, unit_id)
    return unit_detail(workspace, workspace.get_unit(unit_id))


# ------------------------------------------------------------------ 导入


def safe_filename(raw: str) -> str:
    """文件名只作为数据处理：去掉 Windows 与 Unix 路径成分与控制字符。"""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"<>|:*?').strip()
    if not name:
        raise ValidationError("缺少有效的文件名。")
    if len(name) > 200:
        name = name[:200]
    return name


def _magic_ok(path: Path, suffix: str) -> bool:
    """便宜的内容校验：PDF 必须带 %PDF- 头，DOCX 必须是 zip 容器。"""
    with path.open("rb") as handle:
        head = handle.read(1024)
    if suffix == ".pdf":
        return _PDF_MAGIC in head
    return head[:4] == _DOCX_MAGIC


async def _stage_upload(workspace: Workspace, upload: UploadFile,
                        limit: int) -> dict[str, Any]:
    filename = safe_filename(upload.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValidationError(
            f"不支持的文件类型：{suffix or '（无扩展名）'}；当前仅支持 "
            f"{'、'.join(SUPPORTED_EXTENSIONS)}。"
        )
    workspace.staging_dir.mkdir(parents=True, exist_ok=True)
    staging_id = f"stg_{uuid.uuid4().hex[:16]}"
    # 暂存文件名是随机 ID；用户的原始文件名保存在数据库里。
    target = workspace.staging_dir / f"{staging_id}{suffix}"
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValidationError(
                        f"文件 {filename} 超过单文件上限 "
                        f"{limit // (1024 * 1024)}MiB。"
                    )
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if size == 0:
        target.unlink(missing_ok=True)
        raise ValidationError(f"文件 {filename} 是空文件。")
    if not _magic_ok(target, suffix):
        target.unlink(missing_ok=True)
        raise ValidationError(
            f"文件 {filename} 的内容不是有效的 {suffix} 文档（文件头校验未通过）。"
        )
    return {
        "id": staging_id,
        "filename": filename,
        "stored_path": target.relative_to(workspace.root).as_posix(),
        "sha256": digest.hexdigest(),
        "byte_size": size,
    }


@router.post("/imports", status_code=202)
async def create_imports(request: Request,
                        files: list[UploadFile] = File(...),
                        ocr_mode: str = Form("auto")) -> JSONResponse:
    """流式暂存多个文件并入队；每个文件独立校验，失败只影响它自己。"""
    workspace = workspace_of(request)
    store = jobs_of(request)
    mode = validate_ocr_mode(ocr_mode)
    job_ids: list[str] = []
    rejected: list[dict[str, str]] = []
    for upload in files:
        try:
            label = safe_filename(upload.filename or "未命名文件")
        except ValidationError as exc:
            rejected.append({"filename": "未命名文件", "error": str(exc)})
            await upload.close()
            continue
        try:
            staged = await _stage_upload(workspace, upload, MAX_UPLOAD_BYTES)
        except LitError as exc:
            rejected.append({"filename": label, "error": str(exc)})
            await upload.close()
            continue
        store.add_staging(staged["id"], staged["filename"], staged["stored_path"],
                          staged["sha256"], staged["byte_size"])
        job = store.enqueue_import(staged["id"], staged["filename"], mode)
        job_ids.append(job["id"])
    if not job_ids:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "ALL_FILES_REJECTED",
                               "message": "所有文件都被拒绝，详见 rejected。",
                               "details": {"rejected": rejected}}},
        )
    return JSONResponse(status_code=202, content={"job_ids": job_ids, "rejected": rejected})


@router.post("/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: str, request: Request, body: ReprocessRequest) -> dict[str, Any]:
    """对已完成的页强制重新识别：范围外的页不动，人工校订与证据保留。"""
    workspace = workspace_of(request)
    document = workspace.get_document(doc_id)
    store = jobs_of(request)
    if document["filetype"] != "pdf":
        raise ValidationError("重新识别目前只支持 PDF；Word 请使用校订。")
    job = store.enqueue_reprocess(doc_id, body.unit_ids, mode=body.mode,
                                 keep_corrections=body.keep_corrections,
                                 filename=document["filename"])
    return {"job_ids": [job["id"]], "job": job}
