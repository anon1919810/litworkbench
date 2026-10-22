"""OCR 候选存储、页内容修订号与采用事务（Task A，网页里程碑）。

这些测试只使用 ``litworkbench.demo`` 生成的**合成**夹具与真实 SQLite
（临时工作区），不运行任何真实 OCR 模型；受控推理由 Task B 覆盖。

候选服务（``litworkbench.ocr_candidates.CandidateStore``）与迁移 7 尚未实现时，
本模块会在导入阶段就失败——这是本轮刻意保留的 RED。

冻结接口见 docs/web-ocr-candidate-contract.md 与
docs/superpowers/plans/2026-10-16-ocr-candidate-review.md（Task A）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from litworkbench import migrations, search_index
from litworkbench.errors import (
    EditConflictError,
    LitError,
    NotFoundError,
    ValidationError,
)
from litworkbench.evidence import (
    correct_unit,
    get_evidence,
    review_evidence,
    save_evidence,
)
from litworkbench.ingest import import_document, search as ingest_search
from litworkbench.jobs import JobStore
from litworkbench.ocr_candidates import CandidateStore
from litworkbench.store import Workspace, sha256_text, utcnow
from litworkbench.text import normalize

PROFILE = "small-lowmem-v1"
LAYOUT = "original"

#: 候选文字：含「省级」，与原页文字「县级文物保护单位」不同，便于判定索引。
RESULT_TEXT = "候选识别：白塔为省级文物保护单位。"
#: 只出现在采用前的旧机器底稿里，用来验证「旧 OCR 快照」真的被保留。
PRIOR_OCR_MARKER = "prior-ocr-marker-3f7a"

RESULT = {
    "text": RESULT_TEXT,
    "lines": [
        {"box": [[10, 10], [110, 10], [110, 40], [10, 40]],
         "text": RESULT_TEXT, "confidence": 0.91},
    ],
    "engine": "rapidocr",
    "model_fingerprints": [{"name": "rec", "sha256": "r" * 16}],
    "config": {"profile_id": PROFILE, "render_scale": 2},
    "timing": {"init_seconds": 0.4, "recognize_seconds": 1.1},
    "memory": {"rss_bytes": 123456, "private_bytes": 654321},
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
def ocr_fixtures(tmp_path_factory):
    """合成 PDF 只在整模块生成一次，避免每个用例都重跑 reportlab。"""
    from litworkbench import demo

    return demo.make_fixtures(tmp_path_factory.mktemp("合成 候选夹具"))


@pytest.fixture
def store(workspace):
    return CandidateStore(workspace)


@pytest.fixture
def jobs_store(workspace):
    return JobStore(workspace)


@pytest.fixture
def pdf_doc(workspace, ocr_fixtures):
    """三页文字版 PDF：method='text'，不需要任何 OCR。"""
    return import_document(workspace, ocr_fixtures["text_pdf"], ocr_mode="off")


# ------------------------------------------------------------------ 助手


def _candidate_of(result):
    """adopt 返回 {candidate, unit}；其余方法直接返回候选详情。"""
    if isinstance(result, dict) and isinstance(result.get("candidate"), dict):
        return result["candidate"]
    return result


def _revision(workspace, unit_id) -> int:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT content_revision FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
    assert row is not None, unit_id
    return int(row["content_revision"])


def _generation(workspace, job_id) -> int:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT claim_generation FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None, job_id
    return int(row["claim_generation"])


def _begin(store, workspace, job_id, unit_id, owner):
    generation = _generation(workspace, job_id)
    candidate = _candidate_of(
        store.begin_attempt(job_id, unit_id, owner=owner, generation=generation)
    )
    return candidate, generation


def _claim_and_begin(store, jobs_store, workspace, job_id, unit_id, *, owner=None):
    owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
    claimed = jobs_store.claim_next(owner)
    assert claimed is not None and claimed["id"] == job_id, claimed
    candidate, generation = _begin(store, workspace, job_id, unit_id, owner)
    return candidate, owner, generation


def _ready_candidate_for(store, jobs_store, workspace, pdf_doc, *, unit_index=0):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[unit_index]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    store.complete_attempt(
        candidate["id"], RESULT, owner=owner, generation=generation
    )
    return unit_id, job_id, store.get(candidate["id"])


def _add_page_units(workspace, doc_id: str, count: int, *, start_seq: int = 1000) -> list[str]:
    """直接插入合成页单元，用于「超过 1000 页」等不需要真实 PDF 的边界。"""
    now = utcnow()
    rows = []
    ids = []
    for offset in range(count):
        seq = start_seq + offset
        unit_id = f"u_{doc_id}_page_{seq:04d}"
        ids.append(unit_id)
        rows.append((
            unit_id, doc_id, seq, "page", seq, "", "", "text", "completed",
            "completed", "not_needed", f"合成页 {seq}", None, "", None,
            f"合成页 {seq}", "", "", 0, "none", now, now,
        ))
    with workspace.connect() as conn:
        conn.executemany(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, printed_page, "
            "title_path, method, status, extraction_status, ocr_decision, "
            "original_text, corrected_text, notes, ocr_json, search_text, "
            "search_text_corrected, text_sha256, blank_confirmed, "
            "printed_page_status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return ids


def _db_text_tables(workspace):
    with workspace.connect() as conn:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    for row in rows:
        name = row["name"]
        if name.startswith("sqlite_") or name.startswith("unit_fts"):
            continue
        if row["sql"] and "VIRTUAL TABLE" in row["sql"].upper():
            continue
        yield name


def _db_contains(workspace, needle: str) -> bool:
    """在整库任意文本列里查找 needle（不假设快照表的内部命名）。"""
    for table in _db_text_tables(workspace):
        with workspace.connect() as conn:
            columns = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if not columns:
                continue
            where = " OR ".join(f'instr(CAST("{c}" AS TEXT), ?) > 0' for c in columns)
            try:
                found = conn.execute(
                    f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1',
                    [needle] * len(columns),
                ).fetchone()
            except sqlite3.OperationalError:  # pragma: no cover - 特殊表跳过
                continue
        if found is not None:
            return True
    return False


# ------------------------------------------------------------------ 迁移 7


def test_schema_version_includes_candidate_migration():
    assert migrations.SCHEMA_VERSION == 8


def test_migration_v6_to_v7_preserves_rows_and_is_idempotent(tmp_path, monkeypatch):
    """真正的 v6 工作区升到 v7：既有文献/单元/摘录/集合一行不丢，且可重复执行。"""
    assert migrations.SCHEMA_VERSION == 8
    # 造一个**真正的** v6 工作区：只登记 2..6，避免 v7 迁移也被跑掉。
    full_migrations = migrations.MIGRATIONS
    monkeypatch.setattr(
        migrations, "MIGRATIONS",
        tuple(m for m in full_migrations if m.version <= 6),
    )
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 6)
    workspace = Workspace(tmp_path / "第六版 工作区" / "文献库")
    workspace.initialize()

    now = utcnow()
    with workspace.connect() as conn:
        version = int(conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"])
        assert version == 6
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
        assert "content_revision" not in columns  # 确实是 v6 结构
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) VALUES "
            "('doc_old', :sha, 'x', 'sources/old.pdf', '旧文献.pdf', 'pdf', "
            "'completed', 'off', :now, :now)",
            {"sha": "a" * 64, "now": now},
        )
        conn.execute(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, method, status, "
            "ocr_decision, original_text, created_at, updated_at) VALUES "
            "('u_old', 'doc_old', 1, 'page', 1, 'text', 'completed', 'not_needed', "
            "'旧工作区的白塔记载', :now, :now)",
            {"now": now},
        )
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, quote, start_offset, end_offset, "
            "version, version_snapshot, created_at, updated_at) VALUES "
            "('ev_old', 'u_old', 'doc_old', '白塔', 0, 2, 'original', '{}', :now, :now)",
            {"now": now},
        )
        conn.execute(
            "INSERT INTO collections (id, name, name_key, description, created_at, "
            "updated_at) VALUES ('col_old', '旧专题', '旧专题', '', :now, :now)",
            {"now": now},
        )
        conn.execute(
            "INSERT INTO collection_evidence (collection_id, evidence_id, added_at) "
            "VALUES ('col_old', 'ev_old', :now)",
            {"now": now},
        )

    monkeypatch.setattr(migrations, "MIGRATIONS", full_migrations)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 8)
    result = migrations.ensure_schema(workspace)
    assert result["from"] == 6 and result["to"] == 8
    assert result["applied"], "迁移 7 必须真正运行"

    with workspace.connect() as conn:
        document = conn.execute("SELECT * FROM documents WHERE id = 'doc_old'").fetchone()
        unit = conn.execute("SELECT * FROM units WHERE id = 'u_old'").fetchone()
        evidence = conn.execute("SELECT * FROM evidence WHERE id = 'ev_old'").fetchone()
        membership = conn.execute(
            "SELECT COUNT(*) AS n FROM collection_evidence WHERE collection_id = 'col_old'"
        ).fetchone()["n"]
        unit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        job_schema = {row["name"]: row for row in conn.execute("PRAGMA table_info(jobs)")}
    assert document is not None and document["filename"] == "旧文献.pdf"
    assert unit is not None
    assert unit["original_text"] == "旧工作区的白塔记载"
    assert unit["content_revision"] == 0            # 旧页初始化为 0
    assert evidence is not None and evidence["quote"] == "白塔"
    assert membership == 1                          # 集合关系保留
    assert "content_revision" in unit_columns
    assert "claim_generation" in job_columns
    assert str(job_schema["claim_generation"]["dflt_value"]).strip("'\"") == "0"

    second = migrations.ensure_schema(workspace)
    assert second["applied"] == [] and second["from"] == 8 and second["to"] == 8


# ------------------------------------------------------------------ 内容修订号


def test_content_revision_advances_on_content_changes(workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    base = _revision(workspace, unit_id)

    with workspace.connect() as conn:
        conn.execute("UPDATE units SET corrected_text = ? WHERE id = ?", ("直接 SQL 校订", unit_id))
    assert _revision(workspace, unit_id) == base + 1

    with workspace.connect() as conn:
        conn.execute("UPDATE units SET blank_confirmed = 1 WHERE id = ?", (unit_id,))
    assert _revision(workspace, unit_id) == base + 2

    with workspace.connect() as conn:
        conn.execute("UPDATE units SET original_text = ? WHERE id = ?", ("机器 OCR 重新写回", unit_id))
    assert _revision(workspace, unit_id) == base + 3

    with workspace.connect() as conn:
        conn.execute(
            "UPDATE units SET ocr_json = ? WHERE id = ?",
            (json.dumps({"engine": "rapidocr", "lines": [], "text": "机器 OCR 重新写回"},
                        ensure_ascii=False), unit_id),
        )
    assert _revision(workspace, unit_id) == base + 4


def test_printed_page_and_bibliography_do_not_advance_content_revision(workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    before = workspace.get_unit(unit_id)
    base = _revision(workspace, unit_id)

    workspace.set_printed_page(unit_id, "317")
    workspace.update_document(pdf_doc["id"], title="改名后仍然只有书目变化")

    after = workspace.get_unit(unit_id)
    assert after["printed_page"] == "317"
    assert after["original_text"] == before["original_text"]
    assert after["corrected_text"] == before["corrected_text"]
    assert _revision(workspace, unit_id) == base


def test_rewriting_identical_content_does_not_advance_revision(workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    unit = workspace.get_unit(unit_id)
    base = _revision(workspace, unit_id)

    workspace.save_unit(unit)  # 各列原值回写：内容没变就不该推进修订号

    assert _revision(workspace, unit_id) == base


def test_set_printed_page_does_not_whole_row_overwrite_content(workspace, pdf_doc, monkeypatch):
    """并发采用/校订之后写印刷页码，不能把过期的整行正文写回去。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    original = workspace.get_unit(unit_id)["original_text"]

    with workspace.connect() as conn:
        conn.execute(
            "UPDATE units SET original_text = ? WHERE id = ?",
            ("并发采用后的新正文白塔", unit_id),
        )

    stale = dict(workspace.get_unit(unit_id))
    stale["original_text"] = original          # 陈旧读到的旧正文
    stale["corrected_text"] = None
    stale["ocr_json"] = None
    monkeypatch.setattr(workspace, "get_unit", lambda *a, **k: dict(stale))

    workspace.set_printed_page(unit_id, "88")
    monkeypatch.undo()

    refreshed = Workspace(workspace.root).get_unit(unit_id)
    assert refreshed["original_text"] == "并发采用后的新正文白塔"
    assert refreshed["printed_page"] == "88"


# ------------------------------------------------------------------ 创建候选任务


def test_create_job_does_not_change_text_evidence_or_index(workspace, store, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    search_index.index_unit(workspace, unit_id)
    evidence = save_evidence(workspace, unit_id, "白塔位于县城北侧")
    before_unit = workspace.get_unit(unit_id)
    with workspace.connect() as conn:
        before_revision = search_index.current_revision(conn)
        before_state = dict(conn.execute(
            "SELECT state, text_sha256 FROM index_units WHERE unit_id = ?", (unit_id,)
        ).fetchone())

    job = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE, layout=LAYOUT)

    assert job["id"].startswith("job_")
    assert job["kind"] == "ocr_candidate"
    assert job["doc_id"] == pdf_doc["id"]
    assert job["total_pages"] == 1
    assert "review_pending_pages" in JobStore(workspace).get(job["id"])
    assert JobStore(workspace).get(job["id"])["review_pending_pages"] == 0

    after_unit = workspace.get_unit(unit_id)
    assert after_unit["original_text"] == before_unit["original_text"]
    assert after_unit["corrected_text"] == before_unit["corrected_text"]
    assert after_unit["content_revision"] == before_unit["content_revision"]
    assert get_evidence(workspace, evidence["id"])["review_status"] == "pending"
    with workspace.connect() as conn:
        assert search_index.current_revision(conn) == before_revision
        after_state = dict(conn.execute(
            "SELECT state, text_sha256 FROM index_units WHERE unit_id = ?", (unit_id,)
        ).fetchone())
    assert after_state == before_state


def test_create_job_rejects_empty_units(store, pdf_doc):
    with pytest.raises(ValidationError):
        store.create_job(pdf_doc["id"], [], profile_id=PROFILE)


def test_create_job_rejects_unknown_profile_or_layout(store, workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    with pytest.raises(ValidationError):
        store.create_job(pdf_doc["id"], [unit_id], profile_id="unknown-v9")
    with pytest.raises(ValidationError):
        store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE, layout="two-column")


def test_create_job_rejects_foreign_and_missing_units(store, workspace, pdf_doc, ocr_fixtures):
    other = import_document(workspace, ocr_fixtures["mixed_pdf"], ocr_mode="off")
    foreign = workspace.units_for_doc(other["id"])[0]["id"]
    with pytest.raises((NotFoundError, ValidationError)):
        store.create_job(pdf_doc["id"], [foreign], profile_id=PROFILE)
    with pytest.raises((NotFoundError, ValidationError)):
        store.create_job(pdf_doc["id"], ["u_不存在的页"], profile_id=PROFILE)


def test_create_job_rejects_word_units(store, workspace, ocr_fixtures):
    document = import_document(workspace, ocr_fixtures["docx"], ocr_mode="off")
    unit = workspace.units_for_doc(document["id"])[0]
    assert unit["kind"] != "page"
    with pytest.raises(ValidationError):
        store.create_job(document["id"], [unit["id"]], profile_id=PROFILE)


def test_create_job_rejects_more_than_1000_pages(store, workspace, pdf_doc):
    unit_ids = [unit["id"] for unit in workspace.units_for_doc(pdf_doc["id"])]
    unit_ids += _add_page_units(workspace, pdf_doc["id"], 1001)
    assert len(unit_ids) > 1000
    with pytest.raises(ValidationError):
        store.create_job(pdf_doc["id"], unit_ids, profile_id=PROFILE)


def test_create_job_dedups_units_and_counts_real_pages(store, workspace, pdf_doc):
    units = [unit["id"] for unit in workspace.units_for_doc(pdf_doc["id"])]
    job = store.create_job(
        pdf_doc["id"], [units[0], units[1], units[0], units[2], units[1]],
        profile_id=PROFILE,
    )
    assert job["total_pages"] == 3
    with workspace.connect() as conn:
        stored = conn.execute(
            "SELECT COUNT(*) AS n FROM job_units WHERE job_id = ?", (job["id"],)
        ).fetchone()["n"]
    assert stored == 3


# ------------------------------------------------------------------ 尝试令牌


def test_attempt_requires_active_owner_and_generation(store, jobs_store, workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    owner = "worker-a"
    claimed = jobs_store.claim_next(owner)
    assert claimed is not None and claimed["id"] == job_id
    generation = _generation(workspace, job_id)

    assert store.list_for_unit(unit_id)["total"] == 0
    with pytest.raises(LitError):
        store.begin_attempt(job_id, unit_id, owner="worker-b", generation=generation)
    with pytest.raises(LitError):
        store.begin_attempt(job_id, unit_id, owner=owner, generation=generation + 1)
    assert store.list_for_unit(unit_id)["total"] == 0   # 过期令牌没有留下任何候选

    candidate, generation = _begin(store, workspace, job_id, unit_id, owner)
    with pytest.raises(LitError):
        store.complete_attempt(candidate["id"], RESULT, owner="worker-b", generation=generation)
    with pytest.raises(LitError):
        store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation + 1)

    # 任务已被终结（例如租约被收回）时，旧执行者不能提交结果。
    with workspace.connect() as conn:
        conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job_id,))
    with pytest.raises(LitError):
        store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation)
    assert store.get(candidate["id"])["state"] in {"queued", "running"}


def test_begin_attempt_snapshots_base_revision_and_blocks_second_active_attempt(
    store, jobs_store, workspace, pdf_doc
):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]

    with workspace.connect() as conn:
        conn.execute(
            "UPDATE units SET original_text = original_text || ? WHERE id = ?",
            ("（修订）", unit_id),
        )
    current = _revision(workspace, unit_id)

    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    assert candidate["base_content_revision"] == current
    assert candidate["state"] in {"queued", "running"}
    assert candidate["unit_id"] == unit_id
    assert candidate["doc_id"] == pdf_doc["id"]
    assert candidate["profile_id"] == PROFILE
    assert candidate["layout"] == LAYOUT
    assert candidate["file_page"] == workspace.get_unit(unit_id)["file_page"]
    assert candidate["result"] is None and candidate["error"] is None
    assert candidate["decided_at"] is None

    with pytest.raises((LitError, sqlite3.IntegrityError)):
        store.begin_attempt(job_id, unit_id, owner=owner, generation=generation)


def test_complete_attempt_stores_result_and_marks_ready(store, jobs_store, workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )

    done = _candidate_of(store.complete_attempt(
        candidate["id"], RESULT, owner=owner, generation=generation
    ))
    assert done["state"] == "ready"
    assert done["attempt_id"]
    assert CANDIDATE_DETAIL_FIELDS <= set(done)
    for key in ("text", "lines", "engine", "model_fingerprints", "config", "timing",
                "memory", "coordinate_system", "render_width", "render_height"):
        assert key in done["result"], key
    assert done["result"]["text"] == RESULT_TEXT

    # 候选完成与 job_units 完成在同一事务提交。
    with workspace.connect() as conn:
        checkpoint = conn.execute(
            "SELECT state FROM job_units WHERE job_id = ? AND unit_id = ?",
            (job_id, unit_id),
        ).fetchone()
    assert checkpoint["state"] == "done"
    # 待核 ready 数与推理完成数分开计。
    assert jobs_store.get(job_id)["review_pending_pages"] == 1


def test_complete_attempt_marks_stale_when_unit_changed_while_pending(
    store, jobs_store, workspace, pdf_doc
):
    """推理期间页面被改动：迟到的结果标 stale，绝不覆盖现用正文。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )

    with workspace.connect() as conn:
        conn.execute(
            "UPDATE units SET original_text = ? WHERE id = ?",
            ("推理期间被并发改写的正文", unit_id),
        )

    done = _candidate_of(store.complete_attempt(
        candidate["id"], RESULT, owner=owner, generation=generation
    ))
    assert done["state"] == "stale"
    assert workspace.get_unit(unit_id)["original_text"] == "推理期间被并发改写的正文"


def test_complete_attempt_rejects_empty_or_invalid_result(
    store, jobs_store, workspace, pdf_doc
):
    """空结果或缺少 text 不能混进待核候选；候选保持活动状态。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )

    for bad in ({"text": ""}, {"text": "   "}, {}, None):
        with pytest.raises(ValidationError):
            store.complete_attempt(
                candidate["id"], bad, owner=owner, generation=generation
            )
    assert store.get(candidate["id"])["state"] in {"queued", "running"}


def test_fail_attempt_records_error_and_retry_keeps_failed_record(
    store, jobs_store, workspace, pdf_doc
):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, _generation_used = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )

    failed = _candidate_of(store.fail_attempt(
        candidate["id"], {"kind": "engine_error", "message": "本页识别因内存上限停止"},
        owner=owner, generation=_generation(workspace, job_id),
    ))
    assert failed["state"] == "failed"
    assert failed["error"]["kind"] == "engine_error"

    newer, _ = _begin(store, workspace, job_id, unit_id, owner)
    assert newer["id"] != candidate["id"]

    old = store.get(candidate["id"])
    assert old["state"] == "failed"
    assert old["error"]["message"] == "本页识别因内存上限停止"
    assert store.list_for_unit(unit_id)["total"] == 2


def test_list_for_unit_paginates_and_filters_by_job(store, jobs_store, workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    first_job = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    first, owner1, gen1 = _claim_and_begin(
        store, jobs_store, workspace, first_job, unit_id
    )
    store.complete_attempt(first["id"], RESULT, owner=owner1, generation=gen1)

    body = store.list_for_unit(unit_id)
    assert {"items", "total", "page", "page_size"} <= set(body)
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == first["id"]
    assert "base_content_revision" in item and "state" in item

    second_job = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    _second, _owner2, _gen2 = _claim_and_begin(
        store, jobs_store, workspace, second_job, unit_id
    )
    assert store.list_for_unit(unit_id)["total"] == 2
    assert store.list_for_unit(unit_id, job_id=second_job)["total"] == 1
    assert store.list_for_unit(unit_id, job_id="job_missing")["total"] == 0
    page = store.list_for_unit(unit_id, page=1, page_size=1)
    assert page["page"] == 1 and page["page_size"] == 1 and len(page["items"]) == 1


# ------------------------------------------------------------------ 采用


def test_adopt_updates_machine_text_stales_old_evidence_and_dirties_index(
    store, jobs_store, workspace, pdf_doc
):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    evidence = save_evidence(workspace, unit_id, "白塔位于县城北侧")
    review_evidence(workspace, evidence["id"], "accepted")
    search_index.index_unit(workspace, unit_id)

    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation)
    base = store.get(candidate["id"])["base_content_revision"]

    adopted = store.adopt(candidate["id"], expected_content_revision=base)
    assert {"candidate", "unit"} <= set(adopted)
    assert adopted["candidate"]["state"] == "adopted"
    assert adopted["candidate"]["decided_at"]
    assert adopted["unit"]["content_revision"] == _revision(workspace, unit_id)

    after = workspace.get_unit(unit_id)
    assert after["original_text"] == RESULT_TEXT
    assert after["corrected_text"] is None
    assert after["ocr_json"] is not None
    # 旧原文版摘录依赖的版本真的变了 -> 需复核
    assert get_evidence(workspace, evidence["id"])["review_status"] == "stale"
    # 索引只指向现用文字：采用后该页待重建，且候选文字此时还搜不到
    with workspace.connect() as conn:
        state = conn.execute(
            "SELECT state FROM index_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()["state"]
        pending = search_index.pending_count(conn)
    assert state == "dirty"
    assert pending >= 1
    assert unit_id not in {
        hit["unit_id"] for hit in search_index.search_indexed(workspace, "省级")["items"]
    }
    search_index.index_unit(workspace, unit_id)
    assert unit_id in {
        hit["unit_id"] for hit in search_index.search_indexed(workspace, "省级")["items"]
    }


def test_candidate_text_is_not_searchable_before_adoption(
    store, jobs_store, workspace, pdf_doc
):
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    assert RESULT_TEXT not in workspace.get_unit(unit_id)["text"]
    assert unit_id not in {
        hit["unit_id"] for hit in search_index.search_indexed(workspace, "省级")["items"]
    }


def test_adopt_preserves_correction_page_and_prior_ocr_snapshot(
    store, jobs_store, workspace, pdf_doc
):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    old_machine_text = workspace.get_unit(unit_id)["original_text"]
    corrected_text = "人工校订：白塔位于县城北郊。"
    correct_unit(workspace, unit_id, corrected_text, "校订地名")
    with workspace.connect() as conn:
        conn.execute(
            "UPDATE units SET ocr_json = ? WHERE id = ?",
            (json.dumps({"engine": "prior-engine-xyz", "lines": [],
                         "text": old_machine_text, "marker": PRIOR_OCR_MARKER},
                        ensure_ascii=False), unit_id),
        )
    workspace.set_printed_page(unit_id, "317")
    evidence = save_evidence(workspace, unit_id, corrected_text)
    review_evidence(workspace, evidence["id"], "accepted")

    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation)
    base = store.get(candidate["id"])["base_content_revision"]
    store.adopt(candidate["id"], expected_content_revision=base)

    after = workspace.get_unit(unit_id)
    assert after["original_text"] == RESULT_TEXT            # 只更新机器底稿
    assert after["corrected_text"] == corrected_text        # 人工校订保留
    assert after["text"] == corrected_text                  # 现用文字仍是校订
    assert after["printed_page"] == "317"                   # 印刷页保留
    # 校订文字没变 -> 依赖校订版的摘录不该被无谓废弃
    assert get_evidence(workspace, evidence["id"])["review_status"] == "accepted"
    # 采用前完整旧 OCR 底稿（独立于现用正文）确实被保留
    assert _db_contains(workspace, PRIOR_OCR_MARKER)
    # 旧机器正文进入历史
    revisions = workspace.revisions_for_unit(unit_id)
    assert any(
        r["old_text"] == old_machine_text and r["new_text"] == RESULT_TEXT
        for r in revisions
    ), [dict(r) for r in revisions]


def test_adopt_conflicts_on_stale_or_changed_revision(store, jobs_store, workspace, pdf_doc):
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    base = candidate["base_content_revision"]

    # 期望修订号与候选基础修订号不一致
    with pytest.raises(EditConflictError):
        store.adopt(candidate["id"], expected_content_revision=base + 1)
    assert workspace.get_unit(unit_id)["original_text"] != RESULT_TEXT

    # 候选就绪之后页面内容又被改动：即使期望号=当前号也必须冲突
    with workspace.connect() as conn:
        conn.execute("UPDATE units SET original_text = ? WHERE id = ?", ("并发写入的新正文", unit_id))
    current = _revision(workspace, unit_id)
    with pytest.raises(EditConflictError):
        store.adopt(candidate["id"], expected_content_revision=current)
    assert workspace.get_unit(unit_id)["original_text"] == "并发写入的新正文"
    assert store.get(candidate["id"])["state"] == "ready"


def test_adopt_conflicts_on_source_fingerprint_or_page_change(
    store, jobs_store, workspace, pdf_doc
):
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    base = candidate["base_content_revision"]

    with workspace.connect() as conn:
        conn.execute("UPDATE documents SET sha256 = ? WHERE id = ?", ("f" * 64, pdf_doc["id"]))
    with pytest.raises(EditConflictError):
        store.adopt(candidate["id"], expected_content_revision=base)

    with workspace.connect() as conn:
        conn.execute("UPDATE documents SET sha256 = ? WHERE id = ?",
                     (pdf_doc["sha256"], pdf_doc["id"]))
        conn.execute("UPDATE units SET file_page = 99 WHERE id = ?", (unit_id,))
    with pytest.raises(EditConflictError):
        store.adopt(candidate["id"], expected_content_revision=base)
    assert workspace.get_unit(unit_id)["original_text"] != RESULT_TEXT


def test_adopt_conflicts_when_stored_pdf_bytes_changed(
    store, jobs_store, workspace, pdf_doc
):
    """只有 documents.sha256 未变还不够：实际存储字节被改动也必须拒绝。"""
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    base = candidate["base_content_revision"]

    path = workspace.path_of(workspace.get_document(pdf_doc["id"]))
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n% tampered in place")
        with pytest.raises(EditConflictError):
            store.adopt(candidate["id"], expected_content_revision=base)
    finally:
        path.write_bytes(original)

    assert workspace.get_unit(unit_id)["original_text"] != RESULT_TEXT
    assert store.get(candidate["id"])["state"] == "ready"


def test_adopt_conflicts_when_candidate_is_not_ready(store, jobs_store, workspace, pdf_doc):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, _owner, _gen = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    with pytest.raises(EditConflictError):
        store.adopt(candidate["id"], expected_content_revision=_revision(workspace, unit_id))
    assert store.get(candidate["id"])["state"] in {"queued", "running"}


def test_adopt_rolls_back_on_failure(store, jobs_store, workspace, pdf_doc):
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    base = candidate["base_content_revision"]
    before = workspace.get_unit(unit_id)

    with workspace.connect() as conn:
        conn.executescript(
            "CREATE TRIGGER fail_units_update BEFORE UPDATE ON units "
            "BEGIN SELECT RAISE(ABORT, 'injected adopt failure'); END;"
        )
    try:
        with pytest.raises((sqlite3.IntegrityError, LitError)):
            store.adopt(candidate["id"], expected_content_revision=base)
    finally:
        with workspace.connect() as conn:
            conn.execute("DROP TRIGGER IF EXISTS fail_units_update")

    after = workspace.get_unit(unit_id)
    assert after["original_text"] == before["original_text"]
    assert after["corrected_text"] == before["corrected_text"]
    assert after["content_revision"] == before["content_revision"]
    assert store.get(candidate["id"])["state"] == "ready"


def test_adopt_is_idempotent_and_marks_other_ready_candidates_stale(
    store, jobs_store, workspace, pdf_doc
):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]

    job_a = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    cand_a, owner_a, gen_a = _claim_and_begin(store, jobs_store, workspace, job_a, unit_id)
    store.complete_attempt(cand_a["id"], RESULT, owner=owner_a, generation=gen_a)

    job_b = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    cand_b, owner_b, gen_b = _claim_and_begin(store, jobs_store, workspace, job_b, unit_id)
    store.complete_attempt(cand_b["id"], RESULT, owner=owner_b, generation=gen_b)

    base = store.get(cand_a["id"])["base_content_revision"]
    first = store.adopt(cand_a["id"], expected_content_revision=base)
    assert first["candidate"]["state"] == "adopted"
    decided_at = first["candidate"]["decided_at"]
    revision_after = _revision(workspace, unit_id)

    # 同页其他 ready 候选标为 stale
    assert store.get(cand_b["id"])["state"] == "stale"

    # 同一候选重复采用：返回原决定，不重复写历史或推进版本
    again = store.adopt(cand_a["id"], expected_content_revision=base)
    assert again["candidate"]["state"] == "adopted"
    assert again["candidate"]["decided_at"] == decided_at
    assert _revision(workspace, unit_id) == revision_after
    assert workspace.get_unit(unit_id)["original_text"] == RESULT_TEXT


def test_reject_marks_rejected_and_is_idempotent(store, jobs_store, workspace, pdf_doc):
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    before = workspace.get_unit(unit_id)

    first = _candidate_of(store.reject(candidate["id"], reason="先保留现用"))
    assert first["state"] == "rejected"
    assert first["decided_at"]
    decided_at = first["decided_at"]

    again = _candidate_of(store.reject(candidate["id"], reason="换个原因也仍然幂等"))
    assert again["state"] == "rejected"
    assert again["decided_at"] == decided_at
    assert workspace.get_unit(unit_id)["original_text"] == before["original_text"]

    with pytest.raises(NotFoundError):
        store.reject("candidate_missing")


# ------------------------------------------------------- 采用必须走 canonical save_unit


def test_adopt_writes_canonical_fields_via_save_unit(store, jobs_store, workspace, pdf_doc):
    """采用必须走 ``Workspace.save_unit``：检索字段/文字哈希/ocr_decision/文档计数一起维护。"""
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    store.adopt(candidate["id"], expected_content_revision=candidate["base_content_revision"])

    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT doc_id, original_text, corrected_text, method, ocr_decision, "
            "text_sha256, search_text, search_text_corrected FROM units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        document = conn.execute(
            "SELECT unit_count, pending_count FROM documents WHERE id = ?", (row["doc_id"],)
        ).fetchone()
    assert row["original_text"] == RESULT_TEXT
    assert row["corrected_text"] is None
    assert row["method"] == "ocr"
    assert row["ocr_decision"] == "ocr"                     # 底稿来源标记采用为识别结果
    assert row["text_sha256"] == sha256_text(RESULT_TEXT)    # 现行有效文字哈希
    assert row["search_text"] == normalize(RESULT_TEXT)
    assert row["search_text_corrected"] == ""
    units = workspace.units_for_doc(row["doc_id"])
    assert document["unit_count"] == len(units)
    assert document["pending_count"] == sum(
        1 for u in units if u["status"] != "completed"
    )


def test_adopt_keeps_pending_status_when_correction_is_empty_text(
    store, jobs_store, workspace, pdf_doc
):
    """人工把该页校订成空串时：采用候选后现用文字仍为空 -> status=pending，
    但机器抽取本身成功 -> extraction_status=completed。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    correct_unit(workspace, unit_id, "", "人工确认该页无可录入文字")
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation)
    base = store.get(candidate["id"])["base_content_revision"]

    store.adopt(candidate["id"], expected_content_revision=base)

    after = workspace.get_unit(unit_id)
    assert after["original_text"] == RESULT_TEXT      # 机器底稿已更新
    assert after["corrected_text"] == ""              # 人工校订=空串被保留
    assert after["text"] == ""                        # 现行有效文字仍是空串
    assert after["status"] == "pending"               # 有效文字为空 -> 待处理
    assert after["extraction_status"] == "completed"  # 机器抽取本身成功

    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT text_sha256, search_text, search_text_corrected FROM units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        document = conn.execute(
            "SELECT unit_count, pending_count FROM documents WHERE id = ?",
            (after["doc_id"],),
        ).fetchone()
    assert row["text_sha256"] == sha256_text("")          # 现行有效文字=空
    assert row["search_text"] == normalize(RESULT_TEXT)   # 机器层检索字段仍更新
    assert row["search_text_corrected"] == ""
    units = workspace.units_for_doc(after["doc_id"])
    assert document["unit_count"] == len(units)
    assert document["pending_count"] == sum(
        1 for u in units if u["status"] != "completed"
    )
    assert document["pending_count"] >= 1                 # 该页现在是待处理


def test_content_revision_advances_on_ocr_decision_change(workspace, pdf_doc):
    """``ocr_decision`` 也是内容来源的一部分：只改它也要推进内容修订号。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    base = _revision(workspace, unit_id)
    with workspace.connect() as conn:
        conn.execute("UPDATE units SET ocr_decision = 'ocr' WHERE id = ?", (unit_id,))
    assert _revision(workspace, unit_id) == base + 1


def test_second_adopt_snapshot_captures_first_adopt_provenance(
    store, jobs_store, workspace, pdf_doc
):
    """连续两次采用不同候选。

    第一次采用的**完整**结果元数据（引擎/模型/配置/计时/内存/坐标系）必须落在现用
    ``units.ocr_json``；第二次采用的快照「旧来源」必须来自第一次采用，而不是第二次
    候选自己的配置。
    """
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]

    first = {
        "text": "第一次候选：白塔为省级文物保护单位。",
        "lines": [{"box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                   "text": "第一次候选", "confidence": 0.80}],
        "engine": "engine-alpha",
        "model_fingerprints": [{"name": "rec-a", "sha256": "a" * 16}],
        "config": {"profile_id": "compatible-v1", "render_scale": 2},
        "timing": {"recognize_seconds": 0.5},
        "memory": {"rss_bytes": 111111},
        "coordinate_system": "render_pixels",
        "layout": LAYOUT,
        "render_width": 1000,
        "render_height": 1400,
    }
    job1 = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    cand1, owner1, gen1 = _claim_and_begin(store, jobs_store, workspace, job1, unit_id)
    store.complete_attempt(cand1["id"], first, owner=owner1, generation=gen1)
    store.adopt(cand1["id"], expected_content_revision=store.get(cand1["id"])["base_content_revision"])

    live = workspace.get_unit(unit_id)["ocr_json"]
    assert live["engine"] == "engine-alpha"
    assert live["model_fingerprints"] == first["model_fingerprints"]
    assert live["config"] == first["config"]
    assert live["timing"] == first["timing"]
    assert live["memory"] == first["memory"]
    assert live["coordinate_system"] == "render_pixels"

    second = {
        "text": "第二次候选：白塔为全国重点文物保护单位。",
        "lines": [{"box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                   "text": "第二次候选", "confidence": 0.95}],
        "engine": "engine-beta",
        "model_fingerprints": [{"name": "rec-b", "sha256": "b" * 16}],
        "config": {"profile_id": PROFILE, "render_scale": 3},
        "render_width": 1000,
        "render_height": 1400,
    }
    job2 = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    cand2, owner2, gen2 = _claim_and_begin(store, jobs_store, workspace, job2, unit_id)
    store.complete_attempt(cand2["id"], second, owner=owner2, generation=gen2)
    store.adopt(cand2["id"], expected_content_revision=store.get(cand2["id"])["base_content_revision"])

    with workspace.connect() as conn:
        snap = conn.execute(
            "SELECT engine, model_fingerprints, profile_id, layout, old_ocr_json "
            "FROM ocr_candidate_snapshots WHERE candidate_id = ?", (cand2["id"],)
        ).fetchone()
    assert snap["engine"] == "engine-alpha"
    assert json.loads(snap["model_fingerprints"]) == first["model_fingerprints"]
    assert snap["profile_id"] == "compatible-v1"
    assert snap["layout"] == LAYOUT
    assert "engine-alpha" in snap["old_ocr_json"]


def test_adopt_preserves_malformed_raw_prior_ocr_json(
    store, jobs_store, workspace, pdf_doc
):
    """旧 ``ocr_json`` 是无法解析的坏数据时也要**原样**保留，不能替换成 unknown。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    malformed = "{这不是合法 JSON，但必须原样保留"
    with workspace.connect() as conn:
        conn.execute("UPDATE units SET ocr_json = ? WHERE id = ?", (malformed, unit_id))

    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    store.complete_attempt(candidate["id"], RESULT, owner=owner, generation=generation)
    store.adopt(candidate["id"], expected_content_revision=store.get(candidate["id"])["base_content_revision"])

    with workspace.connect() as conn:
        snap = conn.execute(
            "SELECT old_ocr_json, engine FROM ocr_candidate_snapshots WHERE candidate_id = ?",
            (candidate["id"],),
        ).fetchone()
    assert snap["old_ocr_json"] == malformed
    assert snap["engine"] == "unknown"


def test_reject_refuses_active_candidate(store, jobs_store, workspace, pdf_doc):
    """进行中的尝试不能被丢弃：必须先结束/失败，再决定。"""
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, _owner, _gen = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    with pytest.raises(LitError):
        store.reject(candidate["id"], reason="想丢掉一个进行中的尝试")
    assert store.get(candidate["id"])["state"] in {"queued", "running"}


def test_reject_refuses_adopted_candidate(store, jobs_store, workspace, pdf_doc):
    """已采用的候选仍然受保护，不能被丢弃。"""
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    store.adopt(candidate["id"], expected_content_revision=candidate["base_content_revision"])

    with pytest.raises(EditConflictError):
        store.reject(candidate["id"], reason="不能再丢弃已采用的候选")

    assert store.get(candidate["id"])["state"] == "adopted"
    assert workspace.get_unit(unit_id)["original_text"] == RESULT_TEXT


def test_adopt_updates_legacy_cli_search_fields_and_hash_end_to_end(
    store, jobs_store, workspace, pdf_doc
):
    """采用后，**旧版 CLI 检索**（``ingest.search``，走 search_text/search_text_corrected）
    必须立刻能搜到候选的独有词；text_sha256 与 ocr_decision 也一起更新。

    只查 SQL 里 search_text 是否等于 normalize(text) 还不够：这条用例端到端地证明
    「采用 -> 真正可搜」，正是手工裸写 UPDATE 会漏掉的部分。
    """
    unit_id = workspace.units_for_doc(pdf_doc["id"])[0]["id"]
    unique = "独有候选标记词"
    result = dict(RESULT)
    result["text"] = f"候选识别：白塔为省级文物保护单位（{unique}）。"
    result["lines"] = [{"box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "text": result["text"], "confidence": 0.9}]

    job_id = store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    candidate, owner, generation = _claim_and_begin(
        store, jobs_store, workspace, job_id, unit_id
    )
    store.complete_attempt(candidate["id"], result, owner=owner, generation=generation)
    base = store.get(candidate["id"])["base_content_revision"]

    # 采用前：候选独有词不该出现在旧版检索里（否则这条断言就没有区分度）
    assert all(hit["unit_id"] != unit_id for hit in ingest_search(workspace, unique))

    store.adopt(candidate["id"], expected_content_revision=base)

    assert any(hit["unit_id"] == unit_id for hit in ingest_search(workspace, unique)), (
        "采用后旧版 CLI 检索必须能搜到候选的独有词"
    )
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT text_sha256, search_text, search_text_corrected, ocr_decision "
            "FROM units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        document = conn.execute(
            "SELECT unit_count, pending_count FROM documents WHERE id = ?",
            (workspace.get_unit(unit_id)["doc_id"],),
        ).fetchone()
    assert row["text_sha256"] == sha256_text(result["text"])
    assert row["search_text"] == normalize(result["text"])
    assert row["search_text_corrected"] == ""
    assert row["ocr_decision"] == "ocr"
    units = workspace.units_for_doc(workspace.get_unit(unit_id)["doc_id"])
    assert document["unit_count"] == len(units)
    assert document["pending_count"] == sum(
        1 for u in units if u["status"] != "completed"
    )


def test_first_adopt_snapshot_marks_unknown_prior_provenance(
    store, jobs_store, workspace, pdf_doc
):
    """首次采用（此前没有任何 OCR 底稿）时，快照来源必须是 unknown，而不是套用
    本候选自己的 profile/layout。"""
    unit_id, _job_id, candidate = _ready_candidate_for(store, jobs_store, workspace, pdf_doc)
    store.adopt(candidate["id"], expected_content_revision=candidate["base_content_revision"])

    with workspace.connect() as conn:
        snap = conn.execute(
            "SELECT engine, model_fingerprints, profile_id, layout, old_ocr_json "
            "FROM ocr_candidate_snapshots WHERE candidate_id = ?", (candidate["id"],)
        ).fetchone()
    assert snap["engine"] == "unknown"
    assert snap["model_fingerprints"] == "[]"
    assert snap["profile_id"] == "unknown"     # 不能误标成候选的 small-lowmem-v1
    assert snap["layout"] == "unknown"
    assert snap["old_ocr_json"] == "unknown"
