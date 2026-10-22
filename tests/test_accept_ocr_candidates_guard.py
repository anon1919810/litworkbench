"""Task D 验收脚本（``scripts/accept_ocr_candidates.py``）安全边界单测。

只验证脚本的**只读安全守卫**（路径解析、真实库拒绝、来源/db/output 逃逸、
来源哈希推导、文件哈希）以及“写前守卫失败不打开工作区”。不触发真实 OCR/迁移/写入。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "accept_ocr_candidates.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("accept_ocr_candidates_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _make_clone(root: Path, db_name: str = "litworkbench.sqlite3") -> Path:
    """构造唯一批准形状的克隆目录（不建真实库）。"""
    clone = root / "work" / "ocr-candidates-2026-10-16" / "acceptance-library"
    clone.mkdir(parents=True)
    (clone / "config.toml").write_text(f'[workspace]\ndb_file = "{db_name}"\n', encoding="utf-8")
    (clone / db_name).write_bytes(b"SQLite format 3\x00")
    return clone


def test_search_difference_ignores_whitespace_only_changes(script):
    assert script._distinct_window("甲乙丙丁戊己\n庚辛壬癸", "甲乙丙丁戊己庚辛壬癸") is None
    assert script._distinct_window("甲乙丙丁戊己\n庚辛壬癸", "甲乙丙丁戊己庚新壬癸") is not None


# --------------------------------------------------------------- resolve_clone


def test_resolve_clone_accepts_injected_expected_root(script, tmp_path):
    clone = _make_clone(tmp_path)
    assert script.resolve_clone(clone, expected_root=clone) == clone.resolve()


def test_resolve_clone_rejects_same_shape_but_not_approved(script, tmp_path):
    # 形状完全相同，但不是被批准的克隆 -> 必须拒绝。
    approved = _make_clone(tmp_path / "a")
    impostor = _make_clone(tmp_path / "b")
    assert approved != impostor
    with pytest.raises(script.GuardError):
        script.resolve_clone(impostor, expected_root=approved)


def test_resolve_clone_rejects_none(script):
    with pytest.raises(script.GuardError):
        script.resolve_clone(None, expected_root=Path("x"))


def test_resolve_clone_rejects_missing_config_or_db(script, tmp_path):
    clone = tmp_path / "work" / "ocr-candidates-2026-10-16" / "acceptance-library"
    clone.mkdir(parents=True)  # 缺 config.toml 与 SQLite
    with pytest.raises(script.GuardError):
        script.resolve_clone(clone, expected_root=clone)


# ------------------------------------------------------------------ db_file 逃逸


def test_db_path_for_rejects_escape(script, tmp_path):
    clone = _make_clone(tmp_path)
    (clone / "config.toml").write_text('[workspace]\ndb_file = "../../..//evil.sqlite3"\n', encoding="utf-8")
    with pytest.raises(script.GuardError):
        script.db_path_for(clone)
    # resolve_clone 也必须在写前拒绝
    with pytest.raises(script.GuardError):
        script.resolve_clone(clone, expected_root=clone)


def test_db_path_for_rejects_absolute(script, tmp_path):
    clone = _make_clone(tmp_path)
    (clone / "config.toml").write_text('[workspace]\ndb_file = "C:/evil.sqlite3"\n', encoding="utf-8")
    with pytest.raises(script.GuardError):
        script.db_path_for(clone)


def test_db_path_for_accepts_inside(script, tmp_path):
    clone = _make_clone(tmp_path)
    assert script.db_path_for(clone) == (clone / "litworkbench.sqlite3").resolve()


# --------------------------------------------------- assert_not_real_library


def test_assert_not_real_library_rejects_outputs_library(script, tmp_path):
    real = tmp_path / "outputs" / "library"
    real.mkdir(parents=True)
    with pytest.raises(script.GuardError):
        script.assert_not_real_library(real)


def test_assert_not_real_library_rejects_descendant_of_library_root(script, tmp_path):
    real = tmp_path / "outputs" / "library"
    inside = real / "sources" / "aa"
    inside.mkdir(parents=True)
    with pytest.raises(script.GuardError):
        script.assert_not_real_library(inside, library_root=real)


def test_assert_not_real_library_allows_clone_outside_library(script, tmp_path):
    clone = _make_clone(tmp_path)
    real = tmp_path / "outputs" / "library"
    real.mkdir(parents=True)
    script.assert_not_real_library(clone, library_root=real)  # 不抛错


# --------------------------------------------------- expected_source_hashes


def _samples(entries) -> dict:
    return {"samples": [{"sample_id": f"s{i}", "source_relative_path": p, "source_sha256": d}
                        for i, (p, d) in enumerate(entries)]}


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def test_expected_source_hashes_six_unique(script):
    entries = [(f"sources/{i:02d}/book{i}.pdf", _digest(str(i))) for i in range(6)]
    result = script.expected_source_hashes(_samples(entries))
    assert len(result) == script.EXPECTED_SOURCES == 6
    assert result["sources/00/book0.pdf"] == _digest("0")


@pytest.mark.parametrize("entries", [
    [(f"sources/{i}/b{i}.pdf", _digest(str(i))) for i in range(5)],
    [(f"sources/{i}/b{i}.pdf", _digest("same")) for i in range(6)],
    [(f"sources/{i}/b{i}.pdf", _digest(str(i))) for i in range(5)] + [("sources/5/b5.pdf", "deadbeef")],
    [(f"sources/{i}/b{i}.pdf", _digest(str(i))) for i in range(5)] + [("", _digest("6"))],
])
def test_expected_source_hashes_rejects_bad_sets(script, entries):
    with pytest.raises(script.GuardError):
        script.expected_source_hashes(_samples(entries))


# --------------------------------------------------- source path 逃逸/符号链接


def test_validate_source_paths_rejects_escape(script, tmp_path):
    clone = _make_clone(tmp_path)
    (clone / "sources").mkdir()
    samples = {"samples": [{"sample_id": "ok", "source_relative_path": "sources/a.pdf"},
                           {"sample_id": "bad", "source_relative_path": "../outside.pdf"}]}
    assert script.validate_source_paths(clone, samples) == ["bad"]


def test_validate_source_paths_rejects_symlink(script, tmp_path):
    clone = _make_clone(tmp_path)
    sources = clone / "sources"
    sources.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"x")
    try:
        os.symlink(outside, sources / "link.pdf")
    except (OSError, NotImplementedError):
        pytest.skip("本环境无法创建符号链接")
    samples = {"samples": [{"sample_id": "link", "source_relative_path": "sources/link.pdf"}]}
    assert script.validate_source_paths(clone, samples) == ["link"]


def test_validate_source_paths_accepts_inside(script, tmp_path):
    clone = _make_clone(tmp_path)
    (clone / "sources").mkdir()
    samples = {"samples": [{"sample_id": "ok", "source_relative_path": "sources/a.pdf"}]}
    assert script.validate_source_paths(clone, samples) == []


# --------------------------------------------------------------- resolve_output


def test_resolve_output_accepts_approved_and_subdir(script, tmp_path):
    clone = _make_clone(tmp_path)
    approved = clone.parent
    assert script.resolve_output(None, clone) == approved
    assert script.resolve_output(approved / "reports", clone) == (approved / "reports")


def test_resolve_output_rejects_outside_and_inside(script, tmp_path):
    clone = _make_clone(tmp_path)
    with pytest.raises(script.GuardError):  # 批准目录之外
        script.resolve_output(tmp_path / "elsewhere", clone)
    with pytest.raises(script.GuardError):  # 落在克隆之内
        script.resolve_output(clone, clone)
    with pytest.raises(script.GuardError):  # sources 之内
        script.resolve_output(clone / "sources", clone)


# --------------------------------------------------------------- verify/hash


def test_verify_clone_sources_detects_match_and_mismatch(script, tmp_path):
    good = tmp_path / "good.pdf"
    good.write_bytes(b"hello")
    expected = {"good.pdf": script.sha256_file(good), "missing.pdf": _digest("missing")}
    ok, detail = script.verify_clone_sources(tmp_path, expected)
    assert ok is False
    assert detail["good.pdf"]["ok"] is True and detail["missing.pdf"]["actual"] is None


def test_sha256_file_matches_hashlib(script, tmp_path):
    payload = "验收夹具 内容".encode("utf-8")
    target = tmp_path / "blob.bin"
    target.write_bytes(payload)
    assert script.sha256_file(target) == hashlib.sha256(payload).hexdigest()


# --------------------------------------------------- 写前守卫：失败不打开工作区


def _consistent_fakes():
    counts = [16, 16, 16, 16, 16, 11]  # 合计 91 页
    documents, units, expected, samples = {}, {}, {}, []
    for i, cnt in enumerate(counts):
        doc, digest = f"doc{i}", f"h{i}"
        documents[doc] = {"sha256": digest, "filetype": "pdf"}
        expected[f"sources/{i}.pdf"] = digest
        for p in range(1, cnt + 1):
            units[f"u_{doc}_page_{p:04d}"] = {"doc_id": doc, "file_page": p, "status": "completed",
                                              "original_sha256": "x", "corrected_sha256": None,
                                              "content_revision": 0}
        for p in (1, 2):
            samples.append({"sample_id": f"{doc}-p{p}", "doc_id": doc, "file_page": p,
                            "source_sha256": digest, "source_relative_path": f"sources/{i}.pdf"})
    snap = {"meta": {"schema_version": "6", "index_revision": "197"}, "documents": documents, "units": units}
    return {"samples": samples}, snap, expected


def test_source_mismatch_execute_does_not_open_workspace(script, monkeypatch):
    import litworkbench.store as store

    calls = []

    def fake_open(cls, root):
        calls.append(root)
        raise AssertionError("Workspace.open 不得在写前守卫失败时被调用")

    monkeypatch.setattr(store.Workspace, "open", classmethod(fake_open))
    samples, snap, expected = _consistent_fakes()
    acc = script.Acceptance("execute")
    script.run_execute(acc, Path("C:/nope/acceptance-library"), samples, snap, False, {}, expected)
    assert calls == []
    assert any(f["id"] == "source_hashes" for f in acc.failures)
    assert acc.stages["stage1_clone"]["source_hash_ok"] is False


def test_dry_run_report_does_not_claim_real_acceptance(script, tmp_path):
    report = script.build_report(script.Acceptance("dry-run"), tmp_path, tmp_path, tmp_path, {}, {})
    assert report["required_acceptance_passed"] is False
    assert "未执行" in script.render_markdown(report)


def test_main_rejects_bad_output_before_any_stage(script, monkeypatch, tmp_path):
    clone = script.default_clone_root()
    samples = clone.parent.parent / "ocr-quality" / "2026-10-16" / "samples.json"
    if not (clone.is_dir() and samples.is_file()):
        pytest.skip("未找到冻结克隆/样本")
    ran = []
    monkeypatch.setattr(script, "run_execute", lambda *a, **k: ran.append("execute"))
    monkeypatch.setattr(script, "run_dry", lambda *a, **k: ran.append("dry"))
    rc = script.main(["--workspace", str(clone), "--samples", str(samples),
                      "--output", str(tmp_path / "elsewhere"), "--execute"])
    assert rc == 2 and ran == []
