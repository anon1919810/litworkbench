"""模型自校对：候选只入库、逐字校验、采用/拒绝、密钥不外泄。

全部用 ``tests/fake_provider.py`` 的本地假服务驱动，**不发出任何真实网络请求**。
文档都是 ``litworkbench.demo`` 生成的合成数据。
"""

from __future__ import annotations

import json

import pytest
from fake_provider import FAKE_KEY, FakeChatServer

from litworkbench import cli, migrations, proofread
from litworkbench.errors import NotFoundError, ValidationError
from litworkbench.ingest import import_document
from litworkbench.store import Workspace, utcnow

KEY_ENV = "LITWORKBENCH_TEST_KEY"
#: 只在「密钥不外泄」用例里使用的特征串
SECRET = "sk-proofread-SECRET-do-not-leak-7c1f"

QUOTE = "白塔位于县城北侧"
FIXED = "白塔位于县城北郊"
FENCE = chr(96) * 3


# --------------------------------------------------------------- 夹具


@pytest.fixture
def server(monkeypatch):
    """本地假 OpenAI 兼容服务；密钥走环境变量，绝不出现在配置里。"""
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    with FakeChatServer(script=[]) as fake:
        yield fake


def configure(workspace, server, **overrides):
    """把 provider 指向假服务（写盘，命令行端到端也读得到）。"""
    config = dict(workspace.config)
    provider = dict(config.get("provider") or {})
    provider.update({
        "base_url": server.base_url,
        "model": "fake-model",
        "api_key_env": KEY_ENV,
        "timeout": 5.0,
        "temperature": 0.0,
        "max_output_tokens": 512,
    })
    provider.update(overrides)
    config["provider"] = provider
    workspace.save_config(config)
    return config


@pytest.fixture
def target(workspace, fixtures):
    """含「白塔位于县城北侧」的第 1 页。"""
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    assert QUOTE in (unit["text"] or "")
    return workspace, doc, unit


def candidate(unit, original, suggestion, reason="模型判断"):
    """按单元当前文字算出正确偏移的候选条目。"""
    text = unit["text"] or ""
    start = text.index(original)
    return {"original": original, "suggestion": suggestion,
            "start": start, "end": start + len(original), "reason": reason}


def payload(*items):
    return json.dumps(list(items), ensure_ascii=False)


def dump_database(workspace) -> str:
    """整库全表 dump（含虚拟表），用于断言密钥没有落盘。"""
    chunks: list[str] = []
    with workspace.connect() as conn:
        tables = [row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                chunks.append(f"{table}:" + "|".join(str(value) for value in tuple(row)))
    return "\n".join(chunks)


# ------------------------------------------------------------ 生成与列表


def test_generate_then_list(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED, "地名讹误"))]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 1 and result["failed"] == 0
    assert result["provider"]["model"] == "fake-model"
    # 请求里带上了原文，且 system 提示要求 JSON 数组
    assert QUOTE in server.requests[0]["messages"][1]["content"]
    assert "JSON" in server.requests[0]["messages"][0]["content"]

    items = proofread.list_candidates(workspace, status="pending")
    assert len(items) == 1
    item = items[0]
    text = unit["text"]
    offset = text.index(QUOTE)
    assert (item["start_offset"], item["end_offset"]) == (offset, offset + len(QUOTE))
    assert item["original"] == QUOTE and item["suggestion"] == FIXED
    assert item["status"] == "pending" and item["reason"] == "地名讹误"
    assert item["model"] == "fake-model"
    assert item["offsets_match_text"] is True
    assert "PDF 第 1 页" in item["locator"]
    # 生成候选绝不改正文
    assert workspace.get_unit(unit["id"])["corrected_text"] is None


def test_generate_for_whole_document(target, server):
    """--doc 覆盖整篇：对不上的页零候选并计数，绝不猜测。"""
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED))]

    unit_ids = proofread.resolve_unit_ids(workspace, doc_id=doc["id"])
    assert len(unit_ids) == 3
    assert proofread.resolve_unit_ids(workspace, doc_id=doc["filename"]) == unit_ids

    result = proofread.generate_candidates(workspace, unit_ids)

    assert result["created"] == 1
    assert result["units"][0]["status"] == "ok"
    assert all(item["created"] == 0 for item in result["units"][1:])
    assert sum(sum(item["dropped"].values()) for item in result["units"][1:]) == 2
    assert len(proofread.list_candidates(workspace)) == 1


def test_original_mismatch_is_dropped(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    good = candidate(unit, QUOTE, FIXED)
    liars = dict(good, original="白塔位于县城南侧")   # 与正文切片不一致
    server.script = [payload(liars)]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 0
    assert result["units"][0]["dropped"] == {proofread.ERROR_ORIGINAL_MISMATCH: 1}
    assert proofread.list_candidates(workspace) == []


def test_offset_out_of_range_and_negative_are_dropped(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    good = candidate(unit, QUOTE, FIXED)
    text_length = len(unit["text"])
    server.script = [payload(
        dict(good, start=-3, end=3),                       # 负偏移
        dict(good, start=text_length + 5, end=text_length + 9),  # 越界
        dict(good, start=4, end=4),                        # 空区间
        dict(good, start="0", end=3),                      # 字段类型不对
    )]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 0
    dropped = result["units"][0]["dropped"]
    assert dropped[proofread.ERROR_OFFSET_OUT_OF_RANGE] == 3
    assert dropped[proofread.ERROR_MISSING_FIELD] == 1
    assert proofread.list_candidates(workspace) == []


def test_overlapping_candidates_keep_first_only(target, server):
    """重叠策略：按模型返回顺序先到先得。"""
    workspace, doc, unit = target
    configure(workspace, server)
    first = candidate(unit, QUOTE, FIXED, "整段")
    second = candidate(unit, "县城北侧", "县城北郊", "局部")
    assert first["start"] < second["start"] < first["end"]      # 确实重叠
    server.script = [payload(first, second)]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 1
    assert result["units"][0]["dropped"] == {proofread.ERROR_OVERLAP: 1}
    items = proofread.list_candidates(workspace)
    assert len(items) == 1
    assert items[0]["original"] == QUOTE and items[0]["reason"] == "整段"


def test_empty_suggestion_and_no_change_are_dropped(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    good = candidate(unit, QUOTE, FIXED)
    server.script = [payload(
        dict(good, suggestion="   "),
        dict(good, suggestion=QUOTE),
        {"original": QUOTE, "suggestion": FIXED, "start": good["start"]},  # 缺 end
    )]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 0
    dropped = result["units"][0]["dropped"]
    assert dropped[proofread.ERROR_EMPTY_SUGGESTION] == 1
    assert dropped[proofread.ERROR_NO_CHANGE] == 1
    assert dropped[proofread.ERROR_MISSING_FIELD] == 1


# ------------------------------------------------------------ 解析容错


def test_non_json_response_gives_zero_candidates_and_error_code(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = ["抱歉，我无法完成这个任务。"]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 0
    assert result["failed"] == 1
    assert result["units"][0]["status"] == "error"
    assert result["units"][0]["error"] == proofread.ERROR_NOT_JSON
    assert proofread.list_candidates(workspace) == []


def test_markdown_fenced_json_is_parsed(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    body = payload(candidate(unit, QUOTE, FIXED))
    server.script = [f"好的，候选如下：\n{FENCE}json\n{body}\n{FENCE}\n以上共一条。"]

    result = proofread.generate_candidates(workspace, [unit["id"]])

    assert result["created"] == 1
    assert len(proofread.list_candidates(workspace)) == 1


def test_regeneration_is_idempotent(target, server):
    """同一单元重复生成不产生重复 pending 候选。"""
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED))]   # 每次返回同一份

    first = proofread.generate_candidates(workspace, [unit["id"]])
    second = proofread.generate_candidates(workspace, [unit["id"]])

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["units"][0]["dropped"] == {proofread.ERROR_DUPLICATE: 1}
    assert len(proofread.list_candidates(workspace, status="pending")) == 1


# ----------------------------------------------------------------- 决定


def test_accept_changes_text_and_keeps_original_revision(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    original_text = unit["original_text"]
    effective = unit["text"]
    server.script = [payload(candidate(unit, QUOTE, FIXED))]
    proofread.generate_candidates(workspace, [unit["id"]])
    candidate_id = proofread.list_candidates(workspace)[0]["id"]

    result = proofread.accept_candidate(workspace, candidate_id)

    updated = workspace.get_unit(unit["id"])
    assert updated["corrected_text"] == effective.replace(QUOTE, FIXED)
    assert updated["original_text"] == original_text          # 原始识别结果没被覆盖
    assert result["candidate"]["status"] == "accepted"
    assert result["candidate"]["decided_at"]
    revisions = workspace.revisions_for_unit(unit["id"])
    assert revisions[-1]["kind"] == "correction"
    assert revisions[-1]["old_text"] == effective             # 历史里保留采用前的文字
    assert revisions[-1]["new_text"] == updated["corrected_text"]
    assert candidate_id in revisions[-1]["reason"]
    # 同一条候选不能采用两次
    with pytest.raises(ValidationError):
        proofread.accept_candidate(workspace, candidate_id)


def test_reject_keeps_text_and_marks_status(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED))]
    proofread.generate_candidates(workspace, [unit["id"]])
    candidate_id = proofread.list_candidates(workspace)[0]["id"]

    result = proofread.reject_candidate(workspace, candidate_id, reason="模型判断有误")

    assert result["candidate"]["status"] == "rejected"
    assert result["candidate"]["decision_note"] == "模型判断有误"
    updated = workspace.get_unit(unit["id"])
    assert updated["corrected_text"] is None
    assert updated["text"] == unit["text"]
    assert proofread.list_candidates(workspace, status="pending") == []
    assert len(proofread.list_candidates(workspace, status="rejected")) == 1
    with pytest.raises(ValidationError):
        proofread.reject_candidate(workspace, candidate_id)


def test_accept_refuses_candidate_whose_offsets_went_stale(target, server):
    """生成候选后正文被改动：偏移不再成立，采用必须被拒绝。"""
    from litworkbench.evidence import correct_unit

    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED))]
    proofread.generate_candidates(workspace, [unit["id"]])
    candidate_id = proofread.list_candidates(workspace)[0]["id"]

    correct_unit(workspace, unit["id"], "完全不同的另一段文字", "人工先改过")

    with pytest.raises(ValidationError, match="不一致"):
        proofread.accept_candidate(workspace, candidate_id)
    assert workspace.get_unit(unit["id"])["corrected_text"] == "完全不同的另一段文字"
    assert proofread.list_candidates(workspace)[0]["status"] == "pending"


def test_unknown_candidate_and_status_are_reported(target, server):
    workspace, doc, unit = target
    configure(workspace, server)
    with pytest.raises(NotFoundError):
        proofread.accept_candidate(workspace, "pf_不存在")
    with pytest.raises(NotFoundError):
        proofread.reject_candidate(workspace, "pf_不存在")
    with pytest.raises(ValidationError):
        proofread.list_candidates(workspace, status="bogus")


def test_empty_unit_text_is_skipped_without_calling_model(workspace, fixtures, server):
    workspace_ = workspace
    configure(workspace_, server)
    doc = import_document(workspace_, fixtures["scan_pdf"], ocr_mode="off")
    units = [unit for unit in workspace_.units_for_doc(doc["id"]) if not unit["text"].strip()]
    assert units, "扫描版夹具在没有 OCR 时应当有空白单元"

    result = proofread.generate_candidates(workspace_, [unit["id"] for unit in units])

    assert result["created"] == 0
    assert {item["error"] for item in result["units"]} == {proofread.ERROR_EMPTY_TEXT}
    assert server.hits == 0        # 空白单元根本不发请求


# ------------------------------------------------------------ 密钥安全


def test_secret_never_leaks_anywhere(target, server, monkeypatch, capsys):
    workspace, doc, unit = target
    monkeypatch.setenv(KEY_ENV, SECRET)
    configure(workspace, server)

    server.script = [payload(candidate(unit, QUOTE, FIXED))]
    ok = proofread.generate_candidates(workspace, [unit["id"]])

    # 失败路径：服务端把密钥回显在响应体里（provider 只回状态码）
    server.script = [{"status": 500, "body": {"error": f"bad token {SECRET}"}}]
    failed = proofread.generate_candidates(workspace, [unit["id"]])

    # 命令行路径（生成 + 列表）
    server.script = [payload(candidate(unit, "白塔旁另存残碑一通", "白塔旁另存残碑一方"))]
    cli.main(["--workspace", str(workspace.root), "proofread", unit["id"]])
    cli.main(["--workspace", str(workspace.root), "proofread", "--list"])
    captured = capsys.readouterr()

    assert failed["units"][0]["error"] == proofread.ERROR_PROVIDER
    assert "500" in failed["units"][0]["error_message"]
    assert SECRET not in json.dumps(ok, ensure_ascii=False)
    assert SECRET not in json.dumps(failed, ensure_ascii=False)
    assert SECRET not in captured.out and SECRET not in captured.err
    assert SECRET not in dump_database(workspace)
    assert SECRET not in workspace.config_path.read_text("utf-8")   # 配置里只有变量名
    assert KEY_ENV in workspace.config_path.read_text("utf-8")


# ----------------------------------------------------------------- 迁移


def test_schema_v8_upgrades_to_v9_and_keeps_old_data(tmp_path, monkeypatch):
    full = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        tuple(m for m in full if m.version <= 8))
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 8)
    workspace = Workspace(tmp_path / "第八版 工作区" / "文献库")
    workspace.initialize()

    now = utcnow()
    with workspace.connect() as conn:
        assert int(conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]) == 8
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "proofread_candidates" not in tables          # 确实是 v8 结构
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) VALUES "
            "('doc_old', :sha, 'x', 'sources/old.pdf', '旧文献.pdf', 'pdf', "
            "'completed', 'off', :now, :now)", {"sha": "a" * 64, "now": now})
        conn.execute(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, method, status, "
            "ocr_decision, original_text, created_at, updated_at) VALUES "
            "('u_old', 'doc_old', 1, 'page', 1, 'text', 'completed', 'not_needed', "
            "'旧工作区里的白塔记载', :now, :now)", {"now": now})
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, quote, start_offset, end_offset, "
            "version, version_snapshot, created_at, updated_at) VALUES "
            "('ev_old', 'u_old', 'doc_old', '白塔', 0, 2, 'original', '{}', :now, :now)",
            {"now": now})

    # 只放行到 v9：更高版本（notes-workspace / cloud-auth）不属于本用例的范围
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        tuple(m for m in full if m.version <= 9))
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 9)
    result = migrations.ensure_schema(workspace)

    assert result["from"] == 8 and result["to"] == 9
    assert result["applied"] == ["proofread-candidates"]
    assert result["backup"] and (workspace.root / "backups").exists()

    assert workspace.get_unit("u_old")["original_text"] == "旧工作区里的白塔记载"
    with workspace.connect() as conn:
        evidence = conn.execute("SELECT * FROM evidence WHERE id = 'ev_old'").fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM proofread_candidates").fetchone()["n"]
        version = int(conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"])
    assert evidence["quote"] == "白塔"
    assert count == 0
    assert version == 9
    # 新表真的可用（外键指向 units）
    assert proofread.list_candidates(workspace) == []


# --------------------------------------------------------------- 命令行


def test_cli_generate_list_accept_then_list(target, server, capsys):
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED))]
    base = ["--workspace", str(workspace.root), "proofread"]

    assert cli.main([*base, unit["id"]]) == 0
    generated = capsys.readouterr()
    created = json.loads(generated.out)
    assert created["created"] == 1
    assert "候选只入库" in generated.err                     # 进度在 stderr

    assert cli.main([*base, "--list", "--status", "pending"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] == 1
    candidate_id = listing["candidates"][0]["id"]

    assert cli.main([*base, "--accept", candidate_id]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["candidate"]["status"] == "accepted"
    assert accepted["unit"]["corrected_text"] == unit["text"].replace(QUOTE, FIXED)

    assert cli.main([*base, "--list"]) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["count"] == 1
    assert after["candidates"][0]["status"] == "accepted"


def test_cli_reject_and_error_paths(target, server, capsys):
    workspace, doc, unit = target
    configure(workspace, server)
    server.script = [payload(candidate(unit, QUOTE, FIXED))]
    base = ["--workspace", str(workspace.root), "proofread"]
    cli.main([*base, unit["id"]])
    capsys.readouterr()
    candidate_id = proofread.list_candidates(workspace)[0]["id"]

    assert cli.main([*base, "--reject", candidate_id, "--reason", "不用"]) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["candidate"]["status"] == "rejected"

    # 参数组合错误：退出码 1，错误写 stderr，不抛栈
    assert cli.main([*base, "--accept", candidate_id, unit["id"]]) == 1
    bad = capsys.readouterr()
    assert "不能" in bad.err and bad.out == ""
    assert cli.main([*base, "--status", "pending"]) == 1
    assert "只能与 --list" in capsys.readouterr().err


def test_cli_help_is_chinese(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["proofread", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "校订候选" in out and "示例：" in out          # 中文说明而不是英文占位
    assert "--accept" in out and "--reject" in out and "--list" in out
