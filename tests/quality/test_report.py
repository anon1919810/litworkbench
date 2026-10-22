"""任务 4：本地对照报告（HTML 转义、缺失说明、哈希过期、共同集合）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ocr_quality import common, prepare, report
from tests.quality.conftest import make_sample, write_manifest, write_reference


def _record(**overrides) -> dict:
    base = {
        "record_version": 1,
        "run_id": "run-baseline",
        "profile": "baseline",
        "sample_id": "syn-01",
        "doc_id": "doc_a",
        "file_page": 1,
        "source_filename": "某县志.pdf",
        "source_sha256": "a" * 64,
        "printed_page": "",
        "printed_page_status": "none",
        "input_sha256": "i" * 64,
        "reference_sha256": "r" * 64,
        "reference_review_level": "human_checked",
        "case": "auto",
        "method": "ocr",
        "scale": 2.0,
        "repeats": 3,
        "engine": "rapidocr",
        "engine_version": "3.10.0",
        "runtime_versions": {"rapidocr": "3.10.0"},
        "model_fingerprints": [{"component": "det", "path": "m.onnx", "sha256": "m" * 64,
                                "status": "ok"}],
        "config": {"scale": 2.0, "repeats": 3},
        "text": "白塔位于县城北侧",
        "regions": [{"bbox": [1, 2, 3, 4], "page": 1, "source": "rapidocr"}],
        "coordinate_system": "pixel_top_left",
        "granularity": "line",
        "mean_confidence": 0.98,
        "confidence_available": True,
        "page_pdf": "",
        "raw_output": None,
        "error": None,
        "status": "ok",
        "text_empty": False,
        "synthetic_worker": False,
        "timing": {"init_seconds": 1.0, "render_seconds": [0.2, 0.2, 0.2],
                   "recognize_seconds": [1.0, 1.0, 1.0],
                   "median_recognize_seconds": 1.0, "max_recognize_seconds": 1.0},
        "memory": {"available": False, "peak_rss_bytes": None, "reason": "psutil 未安装",
                   "limit_bytes": 8 * 1024 ** 3, "limit_enforced": False},
        "metrics": {"cer": 0.0, "distance": 0, "reference_chars": 8, "status": "ok"},
        "query_cases": [{"query_id": "q01", "kind": "positive", "query": "白塔", "hit": True,
                         "matched": [{"sample_id": "syn-01", "doc_id": "doc_a", "file_page": 1}]}],
    }
    base.update(overrides)
    return base


def _write_run(run_dir: Path, records: list[dict], *, profile: str = "baseline") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    records = [dict(record, run_id=f"run-{profile}") for record in records]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8")
    meta = {
        "record_version": 1,
        "run_id": f"run-{profile}",
        "profile": profile,
        "status": "ok",
        "started_at": "2026-10-16T00:00:00+00:00",
        "finished_at": "2026-10-16T00:10:00+00:00",
        "config": {"scale": 2.0, "repeats": 3, "mode": "auto"},
        "manifest_sha256": "m" * 64,
        "inputs": {record["sample_id"]: record["input_sha256"] for record in records},
        "references": {record["sample_id"]: record["reference_sha256"] for record in records},
        "scorable": [record["sample_id"] for record in records
                     if record["metrics"]["cer"] is not None],
        "failed": [record["sample_id"] for record in records if record["status"] != "ok"],
        "counts": {"ok": len(records)},
        "environment": {"python": "3.12", "rapidocr": "3.10.0"},
        "max_rss_bytes": 8 * 1024 ** 3,
    }
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return run_dir


@pytest.fixture
def dataset(tmp_path) -> dict:
    """一个最小样本集：1 页有核稿参考 + 1 页只有草稿参考（全部合成图）。"""
    import io

    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    library = tmp_path / "库"
    library.mkdir()
    source = library / "某县志.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(200, 300))
    for _ in range(2):
        image = Image.new("RGB", (200, 300), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        pdf.drawImage(ImageReader(buffer), 0, 0, width=200, height=300)
        image.close()
        pdf.showPage()
    pdf.save()

    out = tmp_path / "eval"
    samples = []
    for index, text in enumerate(["白塔位于县城北侧", "白塔残碑一通"], start=1):
        sample = make_sample(source, sample_id=f"syn-0{index}", doc_id="doc_a", file_page=index)
        ref = write_reference(
            out / "refs" / f"syn-0{index}.json", sample_id=sample["sample_id"], text=text,
            review_level="human_checked" if index == 1 else "draft",
            reviewer="合成测试" if index == 1 else "",
        )
        sample["reference_path"] = ref.relative_to(out).as_posix()
        sample["reference_sha256"] = common.sha256_file(ref)
        sample["printed_page"] = "一二" if index == 1 else ""
        sample["printed_page_status"] = "confirmed" if index == 1 else "none"
        samples.append(sample)
    manifest = write_manifest(out / "samples.json", samples, library_root=library,
                              query_cases=[
        {"query_id": "q01", "kind": "positive", "sample_id": "syn-01", "query": "白塔"},
        {"query_id": "q02", "kind": "positive", "sample_id": "syn-02", "query": "残碑"},
        {"query_id": "q03", "kind": "negative", "sample_id": "syn-01", "query": "紫阳书院"},
    ])
    prepare.materialize_dataset(manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for sample in data["samples"]:
        sample["reading_order"] = {"status": "unreviewed" if sample["sample_id"] == "syn-01"
                                   else "ok", "reviewer": "", "note": ""}
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"manifest": manifest, "out": out, "library": library, "source": source}


def _record_for(dataset: dict, sample_id: str, **overrides) -> dict:
    """按清单里的真实哈希构造结果记录（否则会被判成过期）。"""
    samples = json.loads(Path(dataset["manifest"]).read_text(encoding="utf-8"))["samples"]
    entry = next(item for item in samples if item["sample_id"] == sample_id)
    defaults = {
        "sample_id": sample_id,
        "doc_id": entry["doc_id"],
        "file_page": entry["file_page"],
        "source_filename": entry["source_filename"],
        "source_sha256": entry["source_sha256"],
        "printed_page": entry["printed_page"],
        "printed_page_status": entry["printed_page_status"],
        "input_sha256": common.sha256_file(dataset["out"] / entry["page_pdf"]),
        "reference_sha256": entry["reference_sha256"],
        "page_pdf": entry["page_pdf"],
        "reference_review_level": ("human_checked" if sample_id == "syn-01" else "draft"),
    }
    defaults.update(overrides)
    return _record(**defaults)


def test_report_writes_html_json_csv_with_relative_assets(dataset, tmp_path):
    first = _record_for(dataset, "syn-01")
    second = _record_for(dataset, "syn-02",
                         metrics={"cer": None, "distance": None, "reference_chars": 0,
                                  "status": "unavailable", "reason": "参考稿为 draft"})
    run_dir = _write_run(tmp_path / "runs" / "baseline", [first, second])

    out = report.build_report(dataset["manifest"], [run_dir], tmp_path / "报告 输出")

    html = out.read_text(encoding="utf-8")
    assert (out.parent / "summary.json").exists()
    assert (out.parent / "metrics.csv").exists()
    assert "assets/" in html and "http://" not in html and "https://" not in html
    assert "<script" not in html.lower()
    assert (out.parent / "assets" / "syn-01_s2.png").exists(), "图片必须复制到报告目录"
    assert "assets/syn-01_s2.png" in html
    summary = json.loads((out.parent / "summary.json").read_text(encoding="utf-8"))
    assert summary["runs"][0]["profile"] == "baseline"
    assert summary["comparable_sample_ids"] == ["syn-01"]


def test_report_escapes_reference_and_result_text(dataset, tmp_path):
    hostile = "<script>alert('x')</script> & <img src=x onerror=1>"
    reference = write_reference(dataset["out"] / "refs" / "syn-01.json",
                                sample_id="syn-01", text=hostile)
    data = json.loads(Path(dataset["manifest"]).read_text(encoding="utf-8"))
    data["samples"][0]["reference_path"] = reference.relative_to(dataset["out"]).as_posix()
    data["samples"][0]["reference_sha256"] = common.sha256_file(reference)
    Path(dataset["manifest"]).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

    record = _record_for(dataset, "syn-01", text=hostile,
                         reference_sha256=common.sha256_file(reference))
    run_dir = _write_run(tmp_path / "runs" / "baseline", [record])

    html = report.build_report(dataset["manifest"], [run_dir],
                               tmp_path / "报告").read_text("utf-8")
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "&lt;img" in html


def test_report_marks_missing_failed_and_draft_without_claiming_accuracy(dataset, tmp_path):
    ok = _record_for(dataset, "syn-01")
    failed = _record_for(dataset, "syn-02", status="timeout", text=None,
                         error={"kind": "timeout", "message": "超过 180 秒"},
                         metrics={"cer": None, "distance": None, "reference_chars": 0,
                                  "status": "error"})
    run_dir = _write_run(tmp_path / "runs" / "baseline", [ok, failed])

    html = report.build_report(dataset["manifest"], [run_dir],
                               tmp_path / "报告").read_text("utf-8")
    assert "超过 180 秒" in html
    assert "超时" in html
    assert "未提供指标" in html
    assert "参考稿尚未核稿" in html
    summary = json.loads((tmp_path / "报告" / "summary.json").read_text(encoding="utf-8"))
    run_summary = summary["runs"][0]
    assert run_summary["full_cohort"]["total_pages"] == 2
    assert run_summary["full_cohort"]["scored_pages"] == 1
    assert run_summary["full_cohort"]["failed_samples"] == ["syn-02"]
    assert run_summary["comparable_cohort"]["sample_ids"] == ["syn-01"]
    assert summary["accuracy_claim"]["scorable_pages"] == 1
    assert "不代表整库" in summary["accuracy_claim"]["statement"]


def test_report_marks_stale_inputs_as_expired_instead_of_comparing(dataset, tmp_path):
    baseline = _record_for(dataset, "syn-01")
    candidate = _record_for(dataset, "syn-01", profile="candidate", run_id="run-candidate",
                            engine="docling", input_sha256="stale" * 8,
                            metrics={"cer": 0.5, "distance": 4, "reference_chars": 8,
                                     "status": "ok"})
    runs = [_write_run(tmp_path / "runs" / "baseline", [baseline], profile="baseline"),
            _write_run(tmp_path / "runs" / "candidate", [candidate], profile="candidate")]

    html = report.build_report(dataset["manifest"], runs, tmp_path / "报告").read_text("utf-8")
    assert "过期" in html
    summary = json.loads((tmp_path / "报告" / "summary.json").read_text(encoding="utf-8"))
    candidate = next(item for item in summary["runs"] if item["profile"] == "candidate")
    assert candidate["expired"] is True
    assert candidate["expired_reason"]
    assert summary["comparable_sample_ids"] == []
    assert candidate["full_cohort"]["weighted_cer"] is None, "过期运行的分数不得进入比较"


def test_report_detects_changed_reference_file(dataset, tmp_path):
    record = _record_for(dataset, "syn-01")
    run_dir = _write_run(tmp_path / "runs" / "baseline", [record])

    write_reference(dataset["out"] / "refs" / "syn-01.json", sample_id="syn-01",
                    text="白塔位于城南（改过的参考）", review_level="human_checked")

    html = report.build_report(dataset["manifest"], [run_dir],
                               tmp_path / "报告").read_text("utf-8")
    assert "参考稿已变更" in html
    summary = json.loads((tmp_path / "报告" / "summary.json").read_text(encoding="utf-8"))
    assert summary["manifest_reference_changed"] == ["syn-01"]


def test_report_shows_manual_reading_order_review(dataset, tmp_path):
    record = _record_for(dataset, "syn-01")
    run_dir = _write_run(tmp_path / "runs" / "baseline", [record])

    html = report.build_report(dataset["manifest"], [run_dir],
                               tmp_path / "报告").read_text("utf-8")
    assert "阅读顺序" in html
    assert "未复核" in html
    assert "核对人" in html


def test_report_shows_missed_query_words_per_page(dataset, tmp_path):
    record = _record_for(dataset, "syn-01", text="白塔位于城南", query_cases=[
        {"query_id": "q01", "kind": "positive", "query": "白塔", "hit": True,
         "matched": [{"sample_id": "syn-01", "doc_id": "doc_a", "file_page": 1}]},
        {"query_id": "q09", "kind": "positive", "query": "残碑", "hit": False,
         "reason": "not_found", "expected_page": {"doc_id": "doc_a", "file_page": 1}},
    ])
    run_dir = _write_run(tmp_path / "runs" / "baseline", [record])
    common.dump_json(run_dir / "queries.json", {
        "positives": {"total": 2, "hit": 1, "missed": [record["query_cases"][1]], "cases": []},
        "negatives": {"total": 0, "false_hit": 0, "cases": []},
    })

    html = report.build_report(dataset["manifest"], [run_dir],
                               tmp_path / "报告").read_text("utf-8")
    assert "未命中" in html
    assert "残碑" in html
    assert "not_found" in html


def test_report_joins_persisted_queries_when_records_have_none(dataset, tmp_path):
    """旧结果文件没有逐页用例时，从 queries.json 按样本补齐（命中与漏检都补）。"""
    record = _record_for(dataset, "syn-01", query_cases=[])
    run_dir = _write_run(tmp_path / "runs" / "baseline", [record])
    common.dump_json(run_dir / "queries.json", {
        "positives": {"total": 1, "hit": 0, "cases": [
            {"query_id": "q07", "kind": "positive", "sample_id": "syn-01", "query": "残碑",
             "hit": False, "reason": "not_found"}], "missed": []},
        "negatives": {"total": 0, "false_hit": 0, "cases": []},
    })

    html = report.build_report(dataset["manifest"], [run_dir],
                               tmp_path / "报告").read_text("utf-8")
    assert "残碑" in html and "未命中" in html


def test_report_cli_accepts_runs_directory(dataset, tmp_path):
    record = _record_for(dataset, "syn-01")
    _write_run(tmp_path / "runs" / "baseline", [record])

    assert report.main(["--manifest", str(dataset["manifest"]),
                        "--runs", str(tmp_path / "runs"),
                        "--out", str(tmp_path / "报告2")]) == 0
    assert (tmp_path / "报告2" / "report.html").exists()


def test_report_refuses_overwriting_source_or_library(dataset, tmp_path):
    with pytest.raises(common.SafetyError):
        report.build_report(dataset["manifest"], [tmp_path / "runs"], dataset["out"] / "derived")
    with pytest.raises(common.SafetyError):
        report.build_report(dataset["manifest"], [tmp_path / "runs"], dataset["out"].parent)


def test_report_handles_run_without_results_file(dataset, tmp_path):
    empty = tmp_path / "runs" / "empty"
    empty.mkdir(parents=True)
    html = report.build_report(dataset["manifest"], [empty], tmp_path / "报告").read_text("utf-8")
    assert "没有结果文件" in html
    summary = json.loads((tmp_path / "报告" / "summary.json").read_text(encoding="utf-8"))
    assert summary["runs"][0]["full_cohort"]["total_pages"] == 2
    assert summary["runs"][0]["full_cohort"]["missing_pages"] == ["syn-01", "syn-02"]
    assert summary["runs"][0]["full_cohort"]["weighted_cer"] is None


def test_report_metrics_csv_has_nulls_for_missing_metrics(dataset, tmp_path):
    record = _record_for(dataset, "syn-02", status="unavailable", text=None,
                         error={"kind": "missing_dependency", "message": "没有装 docling"},
                         metrics={"cer": None, "distance": None, "reference_chars": 0,
                                  "status": "unavailable"})
    run_dir = _write_run(tmp_path / "runs" / "candidate", [record], profile="candidate")

    report.build_report(dataset["manifest"], [run_dir], tmp_path / "报告")
    csv_text = (tmp_path / "报告" / "metrics.csv").read_text(encoding="utf-8")
    header = csv_text.splitlines()[0]
    assert header.startswith("profile,sample_id,case,status,cer")
    assert "candidate,syn-02" in csv_text
    assert ",," in csv_text, "缺失指标写空值而不是 0"
