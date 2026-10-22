"""Independent acceptance regressions; all inputs are synthetic."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

from scripts.ocr_quality import common, docling_adapter, prepare, rapidocr_worker, report, run, workers
from tests.quality.conftest import make_sample, write_manifest
from tests.quality.test_report import dataset, _record_for, _write_run
from tests.quality.test_docling_adapter import candidate_env, page_pdf, _run_adapter


@pytest.mark.parametrize("sample_id", ["../../library", "..", "C:escape", "a\\b", "a/b", "CON", "a."])
def test_materialize_rejects_unsafe_identifiers(tmp_path, synthetic_library, sample_id):
    library, data = synthetic_library
    sample = make_sample(data["scan_pdf"], sample_id=sample_id, doc_id="doc", file_page=1)
    manifest = write_manifest(tmp_path / "eval" / "samples.json", [sample], library_root=library)
    with pytest.raises((common.SafetyError, common.ValidationFailure)):
        prepare.materialize_dataset(manifest, scales=(1.0,))


def test_parameterized_ocr_handles_real_numpy_arrays():
    import numpy as np
    raw = SimpleNamespace(txts=np.array(["白塔"]), scores=np.array([.95]),
                          boxes=np.array([[[0, 0], [10, 0], [10, 10], [0, 10]]]))
    shaped = rapidocr_worker.shape_output(raw)
    assert shaped["text"] == "白塔"
    assert shaped["lines"][0]["confidence"] == .95


def test_worker_keeps_temporal_peak_when_process_exits(tmp_path, monkeypatch):
    states = iter([None, None, 0, 0])
    process = SimpleNamespace(pid=12345, poll=lambda: next(states, 0))
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(workers.time, "sleep", lambda _: None)
    samples = iter([300, 100, None])
    def sample(*args):
        value = next(samples)
        if value is None:
            return workers.unavailable_memory("NoSuchProcess")
        return {"available": True, "peak_rss_bytes": value, "samples": 1,
                "limit_bytes": 1000, "reason": None}
    monkeypatch.setattr(workers, "sample_process_tree_memory", sample)
    outcome = workers.run_worker(["synthetic"], timeout=1, log_dir=tmp_path)
    assert outcome["memory"]["available"] is True
    assert outcome["memory"]["peak_rss_bytes"] == 300
    assert outcome["memory"]["samples"] == 2


def test_page_deadline_is_separate_from_initialization_budget(tmp_path):
    code = "import time; print('{\"event\":\"phase\",\"phase\":\"page\",\"index\":0}',flush=True); time.sleep(4)"
    outcome = workers.run_worker([sys.executable, "-c", code], timeout=10,
                                 init_timeout=5, page_timeout=.25, log_dir=tmp_path)
    assert outcome["timed_out"] is True
    assert outcome["timeout_phase"] == "page"
    assert outcome["duration_seconds"] < 3


def test_private_commit_limit_is_enforced_even_when_rss_is_low(tmp_path, monkeypatch):
    process = SimpleNamespace(pid=12345, poll=lambda: None)
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(workers, "terminate_tree", lambda p: None)
    monkeypatch.setattr(workers, "sample_process_tree_memory", lambda *a: {
        "available": True, "peak_rss_bytes": 10, "peak_private_bytes": 200,
        "samples": 1, "reason": None})
    outcome = workers.run_worker(["synthetic"], timeout=1, memory_limit_bytes=100, log_dir=tmp_path)
    assert outcome["killed_reason"] == "private_limit"
    assert outcome["memory"]["peak_private_bytes"] == 200


@pytest.mark.parametrize("asset", ["reference_path", "page_pdf"])
def test_removed_asset_is_stale_without_crashing(dataset, asset):
    data = common.load_json(dataset["manifest"])
    sample = data["samples"][0]
    record = _record_for(dataset, "syn-01")
    (dataset["out"] / sample[asset]).unlink()
    assert report._stale_reason(record, sample, dataset["out"])


def test_common_cohort_cannot_reverse_model_ranking(dataset, tmp_path):
    manifest = common.load_json(dataset["manifest"])
    second = manifest["samples"][1]
    ref_path = dataset["out"] / second["reference_path"]
    ref = common.load_json(ref_path)
    ref.update(review_level="human_checked", reviewer="synthetic")
    common.dump_json(ref_path, ref)
    second["reference_sha256"] = common.sha256_file(ref_path)
    common.dump_json(dataset["manifest"], manifest)
    def rec(sid, distance):
        return _record_for(dataset, sid, metrics={"status": "ok", "cer": distance / 10,
                                                "distance": distance, "reference_chars": 10})
    a = _write_run(tmp_path / "a", [rec("syn-01", 1), rec("syn-02", 9)], profile="a")
    b = _write_run(tmp_path / "b", [rec("syn-01", 2)], profile="b")
    report.build_report(dataset["manifest"], [a, b], tmp_path / "report")
    result = common.load_json(tmp_path / "report" / "summary.json")
    assert result["comparable_sample_ids"] == ["syn-01"]
    assert [x["comparable_cohort"]["weighted_cer"] for x in result["runs"]] == [.1, .2]


@pytest.mark.parametrize("change", [{"reference_path": ""}, {"page_pdf": ""},
                                    {"file_page": 2}, {"doc_id": "new-doc"}])
def test_changed_page_identity_or_missing_paths_expire_scores(dataset, change):
    sample = common.load_json(dataset["manifest"])["samples"][0]
    record = _record_for(dataset, "syn-01")
    assert report._stale_reason(record, dict(sample, **change), dataset["out"])


def test_nonempty_run_directory_is_rejected_before_any_processing(dataset, tmp_path):
    out = tmp_path / "old-run"
    out.mkdir()
    (out / "old.json").write_text("{}", encoding="utf-8")
    with pytest.raises(common.ValidationFailure, match="空目录"):
        run.run_benchmark(dataset["manifest"], out, "rapidocr-current")
    assert (out / "old.json").read_text("utf-8") == "{}"


@pytest.mark.parametrize("adapter", [rapidocr_worker, docling_adapter])
@pytest.mark.parametrize("alias", ["output", "sidecar"])
def test_worker_refuses_hardlink_alias_of_input(tmp_path, adapter, alias):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"original source bytes")
    output = tmp_path / "result.json"
    destination = output if alias == "output" else tmp_path / "result.json.raw.json"
    os.link(source, destination)
    assert adapter.main(["--input", str(source), "--output", str(output)]) == 2
    assert source.read_bytes() == b"original source bytes"


def test_report_rejects_records_left_from_a_different_run(dataset, tmp_path):
    path = _write_run(tmp_path / "old", [_record_for(dataset, "syn-01")])
    meta = common.load_json(path / "run.json")
    meta.update(run_id="new-unavailable-run", status="unavailable")
    common.dump_json(path / "run.json", meta)
    common.dump_json(path / "queries.json", {"positives": {"total": 28, "hit": 28},
                                             "negatives": {"total": 19, "false_hit": 0}})
    report.build_report(dataset["manifest"], [path], tmp_path / "report")
    summary = common.load_json(tmp_path / "report" / "summary.json")
    assert summary["comparable_sample_ids"] == []
    assert summary["runs"][0]["full_cohort"]["weighted_cer"] is None
    assert summary["runs"][0]["queries"]["positives"]["hit"] is None


def test_query_errors_and_unknown_status_are_html_escaped(dataset):
    sample = common.load_json(dataset["manifest"])["samples"][0]
    attack = '<img src=x onerror=alert(1)>'
    record = _record_for(dataset, "syn-01", status=attack,
                         query_cases=[{"query": "白塔", "error": attack}])
    block = report._run_block({"meta": {"profile": "synthetic"}}, record, sample, None, dataset["out"])
    assert attack not in block
    assert "&lt;img" in block


def test_docling_warm_runs_use_pdfium_and_timing_protocol(candidate_env, page_pdf, tmp_path):
    output = tmp_path / "docling.json"
    result = _run_adapter(candidate_env, page_pdf, output, ["--force-ocr", "--repeats", "3", "--scale", "2"])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = common.load_json(output)
    assert len(payload["timing"]["recognize_seconds"]) == 3
    assert payload["config"]["pdf_backend"] == "pypdfium2"
    assert run.summarize_timing(payload["timing"])["repeats"] == 3
