"""OCR 候选网页接口（Task A）。

覆盖冻结契约 docs/web-ocr-candidate-contract.md 的路径、请求校验、CSRF、
content_revision 暴露、采用/丢弃与 409——但**不**跑真实 OCR：候选由真实的
CandidateStore + JobStore 在临时库里驱动到 ready，再进行 HTTP 决定。

候选服务/路由尚未实现时，本模块会在导入阶段就失败——本轮刻意保留的 RED。
"""

from __future__ import annotations

import uuid

import pytest

from litworkbench.ingest import import_document
from litworkbench.jobs import JobStore
from litworkbench.ocr_candidates import CandidateStore

PROFILE = "small-lowmem-v1"
LAYOUT = "original"
RESULT_TEXT = "候选识别：白塔为省级文物保护单位。"

RESULT = {
    "text": RESULT_TEXT,
    "lines": [],
    "engine": "rapidocr",
    "model_fingerprints": [],
    "config": {"profile_id": PROFILE, "render_scale": 2},
    "timing": {"init_seconds": 0.3, "recognize_seconds": 0.9},
    "memory": {"rss_bytes": 1111, "private_bytes": 2222},
    "coordinate_system": "render_pixels",
    "render_width": 1240,
    "render_height": 1754,
}

CANDIDATE_DETAIL_FIELDS = {
    "id", "job_id", "attempt_id", "unit_id", "doc_id", "file_page", "state",
    "base_content_revision", "profile_id", "layout", "result", "error",
    "created_at", "decided_at",
}


# ------------------------------------------------------------------ 夹具


@pytest.fixture(scope="module")
def candidate_fixtures(tmp_path_factory):
    from litworkbench import demo

    return demo.make_fixtures(tmp_path_factory.mktemp("合成 候选网页夹具"))


@pytest.fixture
def document(web_workspace, candidate_fixtures):
    return import_document(web_workspace, candidate_fixtures["text_pdf"], ocr_mode="off")


@pytest.fixture
def unit_id(web_workspace, document):
    return web_workspace.units_for_doc(document["id"])[0]["id"]


@pytest.fixture
def store(web_workspace):
    return CandidateStore(web_workspace)


@pytest.fixture
def jobs_store(web_workspace):
    return JobStore(web_workspace)


# ------------------------------------------------------------------ 助手


def _candidate_of(result):
    if isinstance(result, dict) and isinstance(result.get("candidate"), dict):
        return result["candidate"]
    return result


def _revision(workspace, unit) -> int:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT content_revision FROM units WHERE id = ?", (unit,)
        ).fetchone()
    return int(row["content_revision"])


def _active_candidate(store, jobs_store, workspace, doc_id, unit, *, owner=None):
    """通过真实 JobStore 领取任务并开启一次尝试，但**不**提交结果。

    候选停留在 queued/running，用来验证进行中的尝试不能被丢弃。
    """
    owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
    job_id = store.create_job(doc_id, [unit], profile_id=PROFILE)["id"]
    claimed = jobs_store.claim_next(owner)
    assert claimed is not None and claimed["id"] == job_id, claimed
    with workspace.connect() as conn:
        generation = int(conn.execute(
            "SELECT claim_generation FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["claim_generation"])
    candidate = _candidate_of(
        store.begin_attempt(job_id, unit, owner=owner, generation=generation)
    )
    return candidate, owner, generation


def _ready_candidate(store, jobs_store, workspace, doc_id, unit, *, owner=None):
    """通过真实 JobStore 领取并提交一个 ready 候选（不跑 OCR）。"""
    candidate, owner, generation = _active_candidate(
        store, jobs_store, workspace, doc_id, unit, owner=owner
    )
    store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation)
    return store.get(candidate["id"])


# ------------------------------------------------------------------ 路由与校验


def test_create_candidate_job_route_returns_job_envelope(session, document, unit_id):
    response = session.post(
        f"/api/v1/documents/{document['id']}/ocr-candidate-jobs",
        json={"unit_ids": [unit_id], "profile_id": PROFILE, "layout": LAYOUT},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["id"].startswith("job_")
    assert body["kind"] == "ocr_candidate"
    assert body["doc_id"] == document["id"]
    assert "review_pending_pages" in body
    assert body["review_pending_pages"] == 0
    # 直接返回 JobItem，不使用 job_ids 数组或嵌套 job
    assert "job_ids" not in body and "job" not in body


def test_create_candidate_job_route_validates_strictly(session, document, unit_id):
    base = f"/api/v1/documents/{document['id']}/ocr-candidate-jobs"
    unknown_field = session.post(
        base, json={"unit_ids": [unit_id], "profile_id": PROFILE, "bogus": 1}
    )
    assert unknown_field.status_code == 422

    empty = session.post(base, json={"unit_ids": [], "profile_id": PROFILE})
    assert empty.status_code in (400, 422)

    unknown_profile = session.post(base, json={"unit_ids": [unit_id], "profile_id": "nope-v1"})
    assert unknown_profile.status_code == 422

    unknown_layout = session.post(
        base, json={"unit_ids": [unit_id], "profile_id": PROFILE, "layout": "two-column"}
    )
    assert unknown_layout.status_code == 422


def test_candidate_writes_require_csrf(session, document, unit_id):
    response = session.client.post(
        f"/api/v1/documents/{document['id']}/ocr-candidate-jobs",
        json={"unit_ids": [unit_id], "profile_id": PROFILE},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_profiles_endpoint_lists_stable_configs_without_paths(session):
    response = session.get("/api/v1/ocr/profiles")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    ids = {item["id"] for item in items}
    assert {"compatible-v1", "small-lowmem-v1"} <= ids
    for item in items:
        assert {"id", "available"} <= set(item)
        assert isinstance(item["available"], bool)
    assert "C:\\" not in response.text
    assert "/home/" not in response.text


def test_unit_detail_exposes_content_revision(session, unit_id):
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    assert "content_revision" in detail
    assert isinstance(detail["content_revision"], int)


def test_create_candidate_job_does_not_run_ocr(session, web_workspace, document, unit_id):
    before = web_workspace.get_unit(unit_id)
    response = session.post(
        f"/api/v1/documents/{document['id']}/ocr-candidate-jobs",
        json={"unit_ids": [unit_id], "profile_id": PROFILE, "layout": LAYOUT},
    )
    assert response.status_code == 202
    after = web_workspace.get_unit(unit_id)
    assert after["original_text"] == before["original_text"]
    assert after["content_revision"] == before["content_revision"]
    job = JobStore(web_workspace).get(response.json()["id"])
    assert job["kind"] == "ocr_candidate"


# ------------------------------------------------------------------ 候选与决定


def test_list_and_get_candidate_routes(session, web_workspace, store, jobs_store,
                                       document, unit_id):
    candidate = _ready_candidate(store, jobs_store, web_workspace, document["id"], unit_id)

    listed = session.get(f"/api/v1/units/{unit_id}/ocr-candidates").json()
    assert {"items", "total", "page", "page_size"} <= set(listed)
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["id"] == candidate["id"]
    assert "base_content_revision" in item and "state" in item

    filtered = session.get(
        f"/api/v1/units/{unit_id}/ocr-candidates?job_id={candidate['job_id']}"
    ).json()
    assert filtered["total"] == 1

    detail = session.get(f"/api/v1/ocr-candidates/{candidate['id']}").json()
    assert CANDIDATE_DETAIL_FIELDS <= set(detail)
    assert detail["state"] == "ready"
    assert detail["result"]["text"] == RESULT_TEXT


def test_adopt_route_returns_candidate_and_unit_and_is_idempotent(
    session, web_workspace, store, jobs_store, document, unit_id
):
    candidate = _ready_candidate(store, jobs_store, web_workspace, document["id"], unit_id)
    base = candidate["base_content_revision"]

    response = session.post(
        f"/api/v1/ocr-candidates/{candidate['id']}/adopt",
        json={"expected_content_revision": base},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"candidate", "unit"} <= set(body)
    assert body["candidate"]["state"] == "adopted"
    assert body["unit"]["text"] == RESULT_TEXT
    assert body["unit"]["content_revision"] == _revision(web_workspace, unit_id)

    again = session.post(
        f"/api/v1/ocr-candidates/{candidate['id']}/adopt",
        json={"expected_content_revision": base},
    )
    assert again.status_code == 200
    assert again.json()["candidate"]["state"] == "adopted"
    assert web_workspace.get_unit(unit_id)["original_text"] == RESULT_TEXT


def test_adopt_route_requires_expected_revision(session, web_workspace, store, jobs_store,
                                                document, unit_id):
    candidate = _ready_candidate(store, jobs_store, web_workspace, document["id"], unit_id)
    missing = session.post(f"/api/v1/ocr-candidates/{candidate['id']}/adopt", json={})
    assert missing.status_code == 422


def test_adopt_route_returns_409_and_keeps_old_text(session, web_workspace, store, jobs_store,
                                                    document, unit_id):
    candidate = _ready_candidate(store, jobs_store, web_workspace, document["id"], unit_id)
    base = candidate["base_content_revision"]

    # 候选就绪后页面内容被并发改动
    with web_workspace.connect() as conn:
        conn.execute("UPDATE units SET original_text = ? WHERE id = ?", ("并发写入的新正文", unit_id))

    conflict = session.post(
        f"/api/v1/ocr-candidates/{candidate['id']}/adopt",
        json={"expected_content_revision": base},
    )
    assert conflict.status_code == 409, conflict.text
    assert web_workspace.get_unit(unit_id)["original_text"] == "并发写入的新正文"


def test_reject_route_is_idempotent(session, web_workspace, store, jobs_store,
                                    document, unit_id):
    candidate = _ready_candidate(store, jobs_store, web_workspace, document["id"], unit_id)

    first = session.post(
        f"/api/v1/ocr-candidates/{candidate['id']}/reject",
        json={"reason": "先保留现用"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "rejected"
    decided_at = first.json()["decided_at"]

    again = session.post(f"/api/v1/ocr-candidates/{candidate['id']}/reject", json={})
    assert again.status_code == 200
    assert again.json()["state"] == "rejected"
    assert again.json()["decided_at"] == decided_at
    assert web_workspace.get_unit(unit_id)["original_text"] != RESULT_TEXT


def test_reject_route_refuses_active_candidate(session, web_workspace, store, jobs_store,
                                               document, unit_id):
    """接口层面：进行中的尝试（queued/running）不能被丢弃（409），状态保持不变。

    否则一次在途推理完成时会对已被丢弃的候选提交结果而报错；界面只允许对
    ready/failed/stale 决定。
    """
    candidate, _owner, _gen = _active_candidate(
        store, jobs_store, web_workspace, document["id"], unit_id
    )
    assert candidate["state"] in {"queued", "running"}

    response = session.post(
        f"/api/v1/ocr-candidates/{candidate['id']}/reject",
        json={"reason": "想丢掉进行中的尝试"},
    )
    assert response.status_code == 409, response.text
    assert store.get(candidate["id"])["state"] in {"queued", "running"}
