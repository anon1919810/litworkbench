"""OCR 候选的网页接口（Task A）。

路径与响应形状冻结在 ``docs/web-ocr-candidate-contract.md``：

* ``GET  /ocr/profiles``                              可选识别配置（无本机路径）
* ``POST /documents/{doc_id}/ocr-candidate-jobs``     建任务，直接返回 JobItem（202）
* ``GET  /units/{unit_id}/ocr-candidates``            分页列出该页候选
* ``GET  /ocr-candidates/{candidate_id}``             候选详情
* ``POST /ocr-candidates/{candidate_id}/adopt``       采用，返回 ``{candidate, unit}``
* ``POST /ocr-candidates/{candidate_id}/reject``      丢弃，返回候选详情

请求体只接受冻结契约里的字段；``result``/``state`` 等结果字段**不可伪造**。
所有写操作走真实的 :class:`~litworkbench.ocr_candidates.CandidateStore` 事务。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from litworkbench.ocr_candidates import CandidateStore, profile_descriptors
from litworkbench.store import Workspace
from litworkbench.web.documents import unit_detail
from litworkbench.web.pagination import normalize_page
from litworkbench.web.schemas import (
    CandidateAdoptRequest,
    CandidateJobRequest,
    CandidateRejectRequest,
)

router = APIRouter()


def _workspace(request: Request) -> Workspace:
    return request.app.state.workspace


def _store(request: Request) -> CandidateStore:
    store = getattr(request.app.state, "candidates", None)
    if store is None:
        store = CandidateStore(_workspace(request))
        request.app.state.candidates = store
    return store


@router.get("/ocr/profiles")
def list_profiles() -> dict[str, Any]:
    """可选识别配置。本阶段能力探测由 Task B 接入，统一返回不可用与原因。"""
    return {"items": profile_descriptors()}


@router.post("/documents/{doc_id}/ocr-candidate-jobs", status_code=202)
def create_candidate_job(doc_id: str, request: Request,
                         body: CandidateJobRequest) -> dict[str, Any]:
    """建一个识别候选任务（只登记）；直接返回 JobItem，不做识别。"""
    return _store(request).create_job(
        doc_id, body.unit_ids, profile_id=body.profile_id, layout=body.layout
    )


@router.get("/units/{unit_id}/ocr-candidates")
def list_candidates(unit_id: str, request: Request, page: int = Query(1),
                    page_size: int = Query(30),
                    job_id: str | None = Query(None)) -> dict[str, Any]:
    page, page_size = normalize_page(page, page_size)
    return _store(request).list_for_unit(
        unit_id, page=page, page_size=page_size, job_id=job_id
    )


@router.get("/ocr-candidates/{candidate_id}")
def get_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    return _store(request).get(candidate_id)


@router.post("/ocr-candidates/{candidate_id}/adopt")
def adopt_candidate(candidate_id: str, request: Request,
                    body: CandidateAdoptRequest) -> dict[str, Any]:
    workspace = _workspace(request)
    result = _store(request).adopt(
        candidate_id, expected_content_revision=body.expected_content_revision
    )
    return {
        "candidate": result["candidate"],
        "unit": unit_detail(workspace, result["unit"]),
    }


@router.post("/ocr-candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str, request: Request,
                     body: CandidateRejectRequest) -> dict[str, Any]:
    return _store(request).reject(candidate_id, reason=body.reason)
