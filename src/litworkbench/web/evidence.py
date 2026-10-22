"""摘录（证据）接口：精确偏移、版本校验与中文结构化错误。

保存摘录时，哈希校验、切片检查与 INSERT 在**同一个写事务**里完成：

1. ``BEGIN IMMEDIATE`` 先拿到写锁，后台 OCR 或并发校订无法插入中间；
2. 在该事务里读取当前版本文字，校验调用方给的 ``expected_hash``；
3. 校验 ``quote`` 确实是「从 ``start_offset`` 开始的精确切片」，
   同句第二次出现不会误存第一次的位置；
4. 写入并提交。任何一步失败都整体回滚，不留半条记录。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from litworkbench.collections import (
    apply_batch,
    collection_filter_clause,
    plan_batch,
)
from litworkbench.errors import ValidationError
from litworkbench.evidence import REVIEW_STATUSES, get_evidence, save_evidence
from litworkbench.store import Workspace, ensure_write_transaction, utcnow
from litworkbench.web.pagination import normalize_page, offset_of, paginate
from litworkbench.web.schemas import EvidenceBatch, EvidenceCreate, EvidencePatch

router = APIRouter()


def workspace_of(request: Request) -> Workspace:
    return request.app.state.workspace


def evidence_item(workspace: Workspace, record: dict[str, Any]) -> dict[str, Any]:
    """契约里的摘录条目（含来源）。"""
    unit = workspace.get_unit(record["unit_id"])
    document = workspace.get_document(record["doc_id"])
    snapshot = record.get("version_snapshot") or {}
    return {
        "id": record["id"],
        "unit_id": record["unit_id"],
        "doc_id": record["doc_id"],
        "quote": record["quote"],
        "start_offset": record["start_offset"],
        "end_offset": record["end_offset"],
        "version": record["version"],
        "category": record["category"],
        "note": record["note"],
        "review_status": record["review_status"],
        "review_note": record["review_note"],
        "created_at": record["created_at"],
        "source": {
            "filename": document["filename"],
            "title": document["title"],
            "file_page": unit["file_page"],
            "printed_page": unit["printed_page"],
            "printed_page_status": unit.get("printed_page_status") or "none",
            "title_path": unit["title_path"],
            "kind": unit["kind"],
            "seq": unit["seq"],
            "author": document["author"],
            "year": document["year"],
            "unit_text_sha256": snapshot.get("unit_text_sha256", ""),
            "locator": (f"{document['filename']} · "
                        f"{'PDF 第 %s 页' % unit['file_page'] if unit['kind'] == 'page' else 'DOCX 单元 %s' % unit['seq']}"
                        f"{'，印刷页码 %s' % unit['printed_page'] if unit['printed_page'] else ''}"),
        },
    }


@router.post("/evidence", status_code=201)
def create_evidence(request: Request, body: EvidenceCreate) -> dict[str, Any]:
    workspace = workspace_of(request)
    if not body.quote.strip():
        raise ValidationError("引文不能为空。")
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        record = save_evidence(
            workspace,
            body.unit_id,
            body.quote,
            category=body.category,
            note=body.note,
            start_offset=body.start_offset,
            expected_hash=body.expected_hash,
            strict=True,          # 网页端要求精确切片，不接受跨空白的近似匹配
            connection=conn,
        )
    return evidence_item(workspace, record)


@router.get("/evidence")
def list_evidence(request: Request, page: int = Query(1), page_size: int = Query(30),
                  doc_id: str | None = Query(None),
                  category: str | None = Query(None),
                  collection_id: str | None = Query(None),
                  review_status: str | None = Query(None)) -> dict[str, Any]:
    workspace = workspace_of(request)
    page, page_size = normalize_page(page, page_size)
    if review_status and review_status not in REVIEW_STATUSES:
        raise ValidationError(
            f"未知的核验状态：{review_status}（可选 {'/'.join(REVIEW_STATUSES)}）"
        )
    where, params = "", []
    if doc_id:
        where, params = "WHERE doc_id = ?", [doc_id]
    if review_status:
        where += (" AND " if where else "WHERE ") + "review_status = ?"
        params.append(review_status)
    # 分类是精确匹配（空串在列表接口里等于「不筛选」）。
    if category:
        where += (" AND " if where else "WHERE ") + "category = ?"
        params.append(category)
    if collection_id:
        clause, values = collection_filter_clause(collection_id)
        where += (" AND " if where else "WHERE ") + clause
        params.extend(values)
    with workspace.connect() as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM evidence {where}", params).fetchone()["n"])
        rows = conn.execute(
            f"SELECT * FROM evidence {where} ORDER BY created_at DESC, id "
            f"LIMIT ? OFFSET ?",
            [*params, page_size, offset_of(page, page_size)],
        ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["version_snapshot"] = _loads(record["version_snapshot"])
    return paginate([evidence_item(workspace, record) for record in records],
                    total, page, page_size)


@router.post("/evidence/batch")
def batch_evidence(request: Request, body: EvidenceBatch) -> dict[str, Any]:
    """批量设置分类 / 批量加入集合 / 批量移出集合。

    * 只处理请求里**显式给出**的 ID，不会把「当前页」当成「全部结果」；
    * 全部 ID 在写入之前一起校验，任何未知 ID 或未知集合都会整批回滚；
    * 重复加入同一集合是幂等的；没有批量接受核验。
    """
    workspace = workspace_of(request)
    plan = plan_batch(
        evidence_ids=body.evidence_ids,
        category=body.category,
        add_to_collection_id=body.add_to_collection_id,
        remove_from_collection_id=body.remove_from_collection_id,
    )
    return apply_batch(workspace, plan)


@router.get("/evidence/{evidence_id}")
def read_evidence(evidence_id: str, request: Request) -> dict[str, Any]:
    workspace = workspace_of(request)
    return evidence_item(workspace, get_evidence(workspace, evidence_id))


@router.patch("/evidence/{evidence_id}")
def patch_evidence(evidence_id: str, request: Request,
                   body: EvidencePatch) -> dict[str, Any]:
    """只改分类/分析备注/核验状态；引文与偏移永远不会被改写。"""
    workspace = workspace_of(request)
    current = get_evidence(workspace, evidence_id)
    if body.review_status is not None and body.review_status not in REVIEW_STATUSES:
        raise ValidationError(
            f"未知的核验状态：{body.review_status}（可选 {'/'.join(REVIEW_STATUSES)}）"
        )
    changes = body.model_dump(exclude_none=True)
    if changes:
        changes["updated_at"] = utcnow()
        with workspace.connect() as conn:
            ensure_write_transaction(conn)
            conn.execute(
                "UPDATE evidence SET " + ", ".join(f"{key} = ?" for key in changes) + " WHERE id = ?",
                [*changes.values(), evidence_id],
            )
    return evidence_item(workspace, get_evidence(workspace, evidence_id))


def _loads(value: str) -> dict[str, Any]:
    import json

    try:
        return json.loads(value) if value else {}
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - 容错
        return {}
