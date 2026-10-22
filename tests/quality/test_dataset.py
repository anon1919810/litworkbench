"""任务 1：只读清点、派生单页与样本校验（含输出安全边界）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.ocr_quality import common, prepare
from tests.quality.conftest import digest_tree, make_sample, write_manifest, write_reference


def _codes(report: dict) -> set[str]:
    return {item["code"] for item in report["errors"] + report["warnings"]}


def test_prepare_only_inventories_and_never_chooses_samples(tmp_path, synthetic_library):
    """清点不自动挑页、不 OCR，且真实库一个字节都不改。"""
    library, data = synthetic_library
    before = digest_tree(library)
    out = tmp_path / "eval"

    draft = Path(prepare.prepare_dataset(library, out))

    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["readonly"] is True
    assert {doc["filename"] for doc in inventory["documents"]} >= {
        data["text_pdf"].name, data["scan_pdf"].name
    }
    text_pages = [p for p in inventory["pages"] if p.get("library_status_page") == "completed"]
    assert len(text_pages) == 3, "三页文字 PDF 应该在清点里体现为 3 页已完成页"
    assert all(p["library_method"] == "text" for p in text_pages)
    assert any(p["text_layer_meaningful_chars"] > 0 for p in text_pages)
    scan_pages = [p for p in inventory["pages"] if p.get("library_status_page") == "pending"]
    assert scan_pages and scan_pages[0]["text_layer_meaningful_chars"] == 0, \
        "无文字层扫描页的有效字数为 0，可用于挑「坏文本层/需 OCR」候选"

    manifest = json.loads(draft.read_text(encoding="utf-8"))
    assert manifest["samples"] == [], "没有人工选页前不得产生样本"
    assert manifest["query_cases"] == []
    assert manifest["schema_version"] == common.SCHEMA_VERSION
    assert digest_tree(library) == before, "真实库必须保持只读"


def test_prepare_refuses_source_library_and_enclosing_dirs(tmp_path, synthetic_library):
    library, _ = synthetic_library
    for out in (library, library / "eval", library.parent, common.project_root()):
        with pytest.raises(common.SafetyError):
            prepare.prepare_dataset(library, out)


def test_prepare_refuses_symlinked_output_into_library(tmp_path, synthetic_library):
    library, _ = synthetic_library
    link = tmp_path / "指向库"
    try:
        os.symlink(library, link, target_is_directory=True)
    except OSError:  # pragma: no cover - 取决于系统权限
        pytest.skip("当前环境不允许创建目录符号链接")
    with pytest.raises(common.SafetyError):
        prepare.prepare_dataset(library, link)


def test_materialize_requires_explicit_sample_selection(tmp_path, synthetic_library):
    library, data = synthetic_library
    manifest = write_manifest(tmp_path / "eval" / "samples.json", [], library_root=library)
    with pytest.raises(common.ValidationFailure):
        prepare.materialize_dataset(manifest)
    assert not (tmp_path / "eval" / "derived").exists(), "没有选页时不得产出派生文件"


def test_materialize_writes_one_page_pdf_and_scaled_images(tmp_path, synthetic_library):
    library, data = synthetic_library
    out = tmp_path / "eval"
    source = data["text_pdf"]
    before = common.sha256_file(source)
    manifest = write_manifest(out / "samples.json", [
        make_sample(source, sample_id="syn-01", doc_id=data["text_doc"]["id"], file_page=2)
    ], library_root=library)

    path = Path(prepare.materialize_dataset(manifest))

    entry = json.loads(path.read_text(encoding="utf-8"))["samples"][0]
    assert entry["source_sha256"] == before
    assert entry["file_page"] == 2
    page_pdf = out / entry["page_pdf"]
    assert page_pdf.exists() and common.sha256_file(page_pdf) == entry["page_pdf_sha256"]

    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(page_pdf)) as doc:
        assert len(doc) == 1, "派生 PDF 必须恰好一页（候选看到的永远是第 1 页）"

    assert sorted(entry["scales"]) == [2.0, 3.0, 4.0]
    assert len(entry["image_paths"]) == 3
    for relative in entry["image_paths"]:
        image = out / relative
        assert image.exists()
        assert common.sha256_file(image) == entry["image_sha256"][relative]
    assert common.sha256_file(source) == before, "原件不可被改写"


def test_materialize_refuses_to_write_inside_library(tmp_path, synthetic_library):
    library, data = synthetic_library
    manifest = write_manifest(library / "samples.json", [
        make_sample(data["text_pdf"], sample_id="syn-01", doc_id=data["text_doc"]["id"], file_page=1)
    ], library_root=library)
    with pytest.raises(common.SafetyError):
        prepare.materialize_dataset(manifest)


def test_validate_detects_hash_page_and_duplicate_problems(tmp_path, synthetic_library):
    library, data = synthetic_library
    out = tmp_path / "eval"
    good = make_sample(data["text_pdf"], sample_id="syn-01",
                       doc_id=data["text_doc"]["id"], file_page=1)
    bad_hash = make_sample(data["text_pdf"], sample_id="syn-02",
                           doc_id=data["text_doc"]["id"], file_page=2, source_sha256="0" * 64)
    bad_page = make_sample(data["text_pdf"], sample_id="syn-03",
                           doc_id=data["text_doc"]["id"], file_page=99)
    duplicate = make_sample(data["text_pdf"], sample_id="syn-01",
                            doc_id=data["text_doc"]["id"], file_page=3)
    manifest = write_manifest(out / "samples.json", [good], library_root=library)
    prepare.materialize_dataset(manifest)
    materialized = json.loads(Path(manifest).read_text(encoding="utf-8"))["samples"][0]

    bad_hash = make_sample(data["text_pdf"], sample_id="syn-02",
                           doc_id=data["text_doc"]["id"], file_page=2, source_sha256="0" * 64)
    bad_page = make_sample(data["text_pdf"], sample_id="syn-03",
                           doc_id=data["text_doc"]["id"], file_page=99)
    duplicate = make_sample(data["text_pdf"], sample_id="syn-01",
                            doc_id=data["text_doc"]["id"], file_page=3)
    Path(manifest).write_text(json.dumps(
        {**json.loads(Path(manifest).read_text(encoding="utf-8")),
         "samples": [materialized, bad_hash, bad_page, duplicate]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    report = prepare.validate_dataset(manifest)
    assert report["ok"] is False
    codes = _codes(report)
    assert "source_hash_mismatch" in codes
    assert "page_out_of_range" in codes
    assert "duplicate_sample_id" in codes


def test_validate_marks_unreviewed_reference_as_not_scorable(tmp_path, synthetic_library):
    library, data = synthetic_library
    out = tmp_path / "eval"
    draft_ref = write_reference(out / "refs" / "syn-01.json", sample_id="syn-01",
                                text="白塔位于县城北侧", review_level="draft", reviewer="")
    uncertain = write_reference(out / "refs" / "syn-02.json", sample_id="syn-02",
                                text="白塔另存残碑一通", review_level="agent_checked",
                                uncertain_spans=[{"line": 2, "text": "残碑"}])
    checked = write_reference(out / "refs" / "syn-03.json", sample_id="syn-03",
                              text="塔基用条石砌成", review_level="human_checked")
    samples = []
    for index, ref in enumerate((draft_ref, uncertain, checked), start=1):
        sample = make_sample(data["text_pdf"], sample_id=f"syn-0{index}",
                             doc_id=data["text_doc"]["id"], file_page=index)
        sample["reference_path"] = ref.relative_to(out).as_posix()
        sample["reference_sha256"] = common.sha256_file(ref)
        samples.append(sample)
    manifest = write_manifest(out / "samples.json", samples, library_root=library)
    prepare.materialize_dataset(manifest)

    report = prepare.validate_dataset(manifest)
    assert report["summary"]["scorable_samples"] == 1, "只有整页核稿的参考才进 CER 分母"
    assert report["summary"]["reference_review_levels"] == {
        "draft": 1, "agent_checked": 1, "human_checked": 1,
    }
    assert "reference_not_reviewed" in _codes(report)
    assert "uncertain_spans_disable_cer" in _codes(report)


def test_validate_flags_missing_and_symlinked_derived_assets(tmp_path, synthetic_library):
    library, data = synthetic_library
    out = tmp_path / "eval"
    sample = make_sample(data["text_pdf"], sample_id="syn-01",
                         doc_id=data["text_doc"]["id"], file_page=1)
    manifest = write_manifest(out / "samples.json", [sample], library_root=library)
    prepare.materialize_dataset(manifest)
    entry = json.loads(Path(manifest).read_text(encoding="utf-8"))["samples"][0]

    (out / entry["image_paths"][0]).unlink()
    assert "derived_asset_missing" in _codes(prepare.validate_dataset(manifest))

    target = out / entry["image_paths"][1]
    target.unlink()
    try:
        os.symlink(source_of(data), target)
    except OSError:  # pragma: no cover - 取决于系统权限
        pytest.skip("当前环境不允许创建文件符号链接")
    codes = _codes(prepare.validate_dataset(manifest))
    assert "derived_asset_escapes_output_root" in codes or "derived_asset_is_symlink" in codes


def source_of(data: dict) -> Path:
    """库内原件的真实路径（用于制造逃逸符号链接）。"""
    import sqlite3

    conn = sqlite3.connect(str(data["db"]))
    row = conn.execute(
        "SELECT stored_path FROM documents WHERE filename = ?", (data["text_pdf"].name,)
    ).fetchone()
    conn.close()
    return data["db"].parent / row[0]


def test_validate_reports_query_case_coverage_without_claiming_pass(tmp_path, synthetic_library):
    library, data = synthetic_library
    out = tmp_path / "eval"
    samples = [make_sample(data["text_pdf"], sample_id=f"syn-{i:02d}",
                           doc_id=data["text_doc"]["id"], file_page=i) for i in (1, 2)]
    manifest = write_manifest(out / "samples.json", samples, library_root=library, query_cases=[
        {"query_id": "q01", "kind": "positive", "sample_id": "syn-01", "query": "白塔"},
    ])
    prepare.materialize_dataset(manifest)

    report = prepare.validate_dataset(manifest)
    assert report["summary"]["positive_query_cases"] == 1
    assert report["summary"]["negative_query_cases"] == 0
    assert "query_cases_below_plan" in _codes(report)
    assert report["ok"] is False, "样本与检索案例不齐时不得返回“通过”"