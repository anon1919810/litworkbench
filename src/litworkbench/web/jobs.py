"""识别任务接口：只读状态与控制，不在 HTTP 请求里执行识别。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from litworkbench.jobs import JobStore
from litworkbench.web.pagination import normalize_page

router = APIRouter()


def jobs_of(request: Request) -> JobStore:
    return request.app.state.jobs


@router.get("/jobs")
def list_jobs(request: Request, page: int = Query(1), page_size: int = Query(30),
              doc_id: str | None = Query(None)) -> dict[str, Any]:
    store = jobs_of(request)
    page, page_size = normalize_page(page, page_size)
    return store.list(page=page, page_size=page_size, doc_id=doc_id)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    store = jobs_of(request)
    return store.get(job_id)


@router.get("/jobs/{job_id}/units")
def job_units(job_id: str, request: Request, page: int = Query(1),
              page_size: int = Query(30)) -> dict[str, Any]:
    """逐页检查点：失败原因与已完成情况都来自数据库。"""
    from litworkbench.web.pagination import offset_of, paginate

    store = jobs_of(request)
    store.get(job_id)
    page, page_size = normalize_page(page, page_size)
    with store.workspace.connect() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM job_units WHERE job_id = ?", (job_id,)
        ).fetchone()["n"])
        rows = conn.execute(
            "SELECT ju.*, u.file_page, c.id AS candidate_id, c.state AS candidate_state "
            "FROM job_units ju LEFT JOIN units u ON u.id=ju.unit_id "
            "LEFT JOIN ocr_candidates c ON c.id=(SELECT id FROM ocr_candidates "
            "WHERE job_id=ju.job_id AND unit_id=ju.unit_id ORDER BY rowid DESC LIMIT 1) "
            "WHERE ju.job_id = ? ORDER BY ju.position LIMIT ? OFFSET ?",
            (job_id, page_size, offset_of(page, page_size)),
        ).fetchall()
    items = [
        {
            "unit_id": row["unit_id"],
            "position": row["position"],
            "state": row["state"],
            "attempts": row["attempts"],
            "error": row["error"],
            "updated_at": row["updated_at"],
            "file_page": row["file_page"],
            "candidate_id": row["candidate_id"],
            "candidate_state": row["candidate_state"],
        }
        for row in rows
    ]
    return paginate(items, total, page, page_size)


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, request: Request) -> dict[str, Any]:
    """暂停在页面提交的安全点生效；幂等。"""
    return jobs_of(request).request_pause(job_id)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, request: Request) -> dict[str, Any]:
    """继续未完成的页；已成功的页不会重做。幂等。"""
    return jobs_of(request).resume(job_id)


@router.post("/jobs/{job_id}/retry-failed")
def retry_failed(job_id: str, request: Request) -> dict[str, Any]:
    """只重做失败/未完成的页；成功页保持不动。幂等。"""
    return jobs_of(request).retry_failed(job_id)
