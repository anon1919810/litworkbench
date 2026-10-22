"""任务 2/3：受控子进程运行（RapidOCR 基线子进程 + 候选适配器协议）。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_quality import common, metrics, prepare, run
from tests.quality.conftest import digest_tree, make_sample, write_manifest, write_reference

# 协议替身：只用于验证调度与字段映射，**不产生真实识别结果**（记录里带 synthetic 标记）。
FAKE_WORKER = '''
import argparse, json, time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--scale", default="2.0")
parser.add_argument("--repeats", default="1")
parser.add_argument("--params")
parser.add_argument("--init-timeout")
args = parser.parse_args()

name = Path(args.input).parent.name
params = json.loads(Path(args.params).read_text("utf-8")) if args.params else None

if "missingdep" in name:
    print(json.dumps({"error": {"kind": "missing_dependency", "message": "没有装 rapidocr"}}))
    raise SystemExit(3)
if "slow" in name:
    time.sleep(30)

text = "" if "blank" in name else "白塔位于县城北侧，为县级文物保护单位。"
payload = {
    "schema_version": 1,
    "status": "ok",
    "engine": "fake-worker",
    "engine_version": "0",
    "text": text,
    "lines": ([{"box": [[1, 1], [2, 1], [2, 2], [1, 2]], "text": text,
               "confidence": 0.99}] if text else []),
    "regions": ([{"bbox": [1, 1, 2, 2], "page": 1, "source": "fake"}] if text else []),
    "coordinate_system": "pixel_top_left" if text else "none",
    "granularity": "line" if text else "none",
    "mean_confidence": 0.99 if text else None,
    "confidence_available": bool(text),
    "config": {"scale": float(args.scale), "repeats": int(args.repeats), "params": params},
    "timing": {"init_seconds": 0.01,
               "render_seconds": [0.02] * int(args.repeats),
               "recognize_seconds": [0.2] * int(args.repeats)},
    "memory_self_reported": None,
    "model_fingerprints": [{"component": "fake", "path": "", "sha256": None,
                            "status": "synthetic_stub"}],
    "page": {"width_points": 595.0, "height_points": 842.0},
    "raw_output": {"stub": True, "params": params},
}
Path(args.output).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
'''

# 候选替身：块坐标、无置信度，模拟真实候选不能伪造行框与置信度。
FAKE_CANDIDATE = '''
import argparse, json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--force-ocr", action="store_true")
parser.add_argument("--init-timeout", type=float, default=600.0)
parser.add_argument("--scale", type=float, default=2.0)
parser.add_argument("--repeats", type=int, default=3)
args = parser.parse_args()

if "missingdep" in Path(args.input).parent.name:
    print(json.dumps({"error": {"kind": "missing_dependency",
                                "message": "没有安装 docling"}}))
    raise SystemExit(3)

payload = {
    "schema_version": 1,
    "status": "ok",
    "engine": "fake-docling",
    "text": "白塔位于县城北侧",
    "regions": [{"bbox": [10.0, 20.0, 300.0, 40.0], "page": 1, "source": "prov"}],
    "coordinate_system": "pdf_points_top_left",
    "granularity": "block",
    "confidence_available": False,
    "confidence_reason": "候选接口不返回逐块置信度",
    "page": {"width_points": 595.0, "height_points": 842.0},
    "timing": {"init_seconds": 1.5, "render_seconds": None,
               "recognize_seconds": None,
               "reason": "候选自行完成版面与渲染，耗时不可分"},
    "model_fingerprints": [],
    "runtime_versions": {"docling": "0.0.0-fake"},
    "raw_output": {"path": Path(str(args.output) + ".raw.json").name, "sha256": None},
}
Path(str(args.output) + ".raw.json").write_text(json.dumps({"docling": {}}), encoding="utf-8")
Path(args.output).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
'''


@pytest.fixture
def fake_worker(tmp_path) -> Path:
    path = tmp_path / "fake_worker.py"
    path.write_text(FAKE_WORKER, encoding="utf-8")
    return path


@pytest.fixture
def fake_candidate(tmp_path) -> Path:
    path = tmp_path / "fake_candidate.py"
    path.write_text(FAKE_CANDIDATE, encoding="utf-8")
    return path


@pytest.fixture
def eval_manifest(tmp_path, synthetic_library) -> Path:
    """两个样本：文字层页（auto 走直取）与扫描页（auto 走 OCR）。"""
    library, data = synthetic_library
    out = tmp_path / "eval"
    samples = [
        make_sample(data["text_pdf"], sample_id="syn-text", doc_id=data["text_doc"]["id"],
                    file_page=1),
        make_sample(data["scan_pdf"], sample_id="syn-scan", doc_id=data["scan_doc"]["id"],
                    file_page=1),
    ]
    for sample in samples:
        ref = write_reference(out / "refs" / f"{sample['sample_id']}.json",
                              sample_id=sample["sample_id"],
                              text="白塔位于县城北侧，为县级文物保护单位。")
        sample["reference_path"] = ref.relative_to(out).as_posix()
        sample["reference_sha256"] = common.sha256_file(ref)
    manifest = write_manifest(out / "samples.json", samples, library_root=library, query_cases=[
        {"query_id": "q01", "kind": "positive", "sample_id": "syn-text", "query": "白塔"},
        {"query_id": "q02", "kind": "positive", "sample_id": "syn-scan", "query": "白塔"},
        {"query_id": "q03", "kind": "negative", "sample_id": "syn-text", "query": "紫阳书院"},
    ])
    prepare.materialize_dataset(manifest)
    return manifest


def _records(run_dir: Path) -> list[dict]:
    lines = (Path(run_dir) / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _run_json(run_dir: Path) -> dict:
    return json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))


def _run(manifest: Path, out: Path, fake_worker: Path, **kwargs) -> Path:
    return run.run_benchmark(manifest, out, "rapidocr-current",
                             worker_script=fake_worker, **kwargs)


# ------------------------------------------------------------- 基本运行与指纹


def test_baseline_run_writes_results_and_fingerprints(eval_manifest, tmp_path, fake_worker):
    library = eval_manifest.parent.parent / "合成库"
    before = digest_tree(library)

    out = _run(eval_manifest, tmp_path / "run", fake_worker, repeats=2)

    records = _records(out)
    assert len(records) == 2
    meta = _run_json(out)
    assert meta["status"] == "ok"
    assert meta["record_count"] == 2
    assert meta["config"]["repeats"] == 2
    assert set(meta["inputs"]) == {"syn-text", "syn-scan"}
    assert set(meta["references"]) == {"syn-text", "syn-scan"}
    assert all(len(value) == 64 for value in meta["references"].values())
    assert digest_tree(library) == before, "评测不得写回真实库"

    ocr_record = next(r for r in records if r["sample_id"] == "syn-scan")
    assert ocr_record["input_sha256"] == common.sha256_file(
        eval_manifest.parent / ocr_record["page_pdf"])
    assert ocr_record["source_filename"].endswith(".pdf")
    assert ocr_record["file_page"] == 1
    assert ocr_record["status"] == "ok"
    assert ocr_record["case"] == "auto"
    assert ocr_record["granularity"] == "line"
    assert ocr_record["coordinate_system"] == "pixel_top_left"
    assert ocr_record["timing"]["repeats"] == 2
    assert len(ocr_record["timing"]["recognize_seconds"]) == 2
    assert ocr_record["timing"]["median_recognize_seconds"] is not None
    assert ocr_record["timing"]["init_seconds"] == 0.01
    assert ocr_record["metrics"]["cer"] == 0.0
    assert ocr_record["synthetic_worker"] is True, "替身结果必须显式标记"


def test_native_text_case_skips_engine_for_text_layer_pages(eval_manifest, tmp_path, fake_worker):
    out = _run(eval_manifest, tmp_path / "run", fake_worker)

    native = next(r for r in _records(out) if r["sample_id"] == "syn-text")
    assert native["method"] == "text"
    assert native["engine"] == "pdf-text-layer"
    assert "白塔" in native["text"]
    assert native["regions"] == []
    assert native["granularity"] == "none"
    assert native["timing"]["recognize_seconds"] is None
    assert native["timing"]["recognize_reason"] == "该页使用 PDF 文字层，未调用识别引擎"


def test_force_mode_runs_engine_even_with_text_layer(eval_manifest, tmp_path, fake_worker):
    out = _run(eval_manifest, tmp_path / "run", fake_worker, mode="force")
    records = {r["sample_id"]: r for r in _records(out)}
    assert records["syn-text"]["method"] == "ocr"
    assert records["syn-text"]["case"] == "force"
    assert records["syn-text"]["text"] == "白塔位于县城北侧，为县级文物保护单位。"


def test_sample_filter_keeps_full_dataset_identity(eval_manifest, tmp_path, fake_worker):
    out = _run(eval_manifest, tmp_path / "run", fake_worker, sample_ids=["syn-scan"])
    meta = _run_json(out)
    assert [r["sample_id"] for r in _records(out)] == ["syn-scan"]
    assert meta["sample_filter"] == ["syn-scan"]
    assert sorted(meta["dataset_sample_ids"]) == ["syn-scan", "syn-text"], "仍记录数据集全部样本 ID"
    assert set(meta["inputs"]) == {"syn-scan"}


# ------------------------------------------------------------- 失败、超时、缺失


def test_missing_dependency_in_child_is_unavailable_not_success(tmp_path, synthetic_library,
                                                               fake_worker):
    library, data = synthetic_library
    out = tmp_path / "eval"
    sample = make_sample(data["scan_pdf"], sample_id="syn-missingdep", doc_id="doc_x", file_page=1)
    manifest = write_manifest(out / "samples.json", [sample], library_root=library)
    prepare.materialize_dataset(manifest)

    run_dir = _run(manifest, tmp_path / "run", fake_worker)
    record = _records(run_dir)[0]
    assert record["status"] == "unavailable"
    assert record["error"]["kind"] == "missing_dependency"
    assert record["text"] is None
    assert record["metrics"]["cer"] is None
    assert _run_json(run_dir)["status"] == "unavailable"
    assert run.main(["--manifest", str(manifest), "--profile", "rapidocr-current",
                     "--worker-script", str(fake_worker),
                     "--out", str(tmp_path / "run2")]) == 2


def test_timeout_is_persisted_and_other_pages_continue(tmp_path, synthetic_library, fake_worker):
    library, data = synthetic_library
    out = tmp_path / "eval"
    slow = make_sample(data["scan_pdf"], sample_id="syn-slow", doc_id=data["scan_doc"]["id"],
                       file_page=1)
    fast = make_sample(data["text_pdf"], sample_id="syn-fast", doc_id=data["text_doc"]["id"],
                       file_page=2)
    manifest = write_manifest(out / "samples.json", [slow, fast], library_root=library)
    prepare.materialize_dataset(manifest)

    # force 模式：两页都真的启动子进程，超时页不得拖住后面的页。
    run_dir = _run(manifest, tmp_path / "run", fake_worker, page_timeout=2.0, init_timeout=1.0,
                   mode="force")
    records = {r["sample_id"]: r for r in _records(run_dir)}
    assert records["syn-slow"]["status"] == "timeout"
    assert records["syn-slow"]["error"]["kind"] == "timeout"
    assert records["syn-slow"]["timed_out"] is True
    assert records["syn-fast"]["status"] == "ok"
    meta = _run_json(run_dir)
    assert meta["counts"]["timeout"] == 1
    assert meta["counts"]["ok"] == 1
    assert meta["status"] == "partial"


def test_empty_engine_output_is_recorded_and_misses_queries(tmp_path, synthetic_library,
                                                            fake_worker):
    library, data = synthetic_library
    out = tmp_path / "eval"
    blank = make_sample(data["scan_pdf"], sample_id="syn-blank", doc_id="doc_x", file_page=1)
    ref = write_reference(out / "refs" / "syn-blank.json", sample_id="syn-blank",
                          text="白塔位于县城北侧")
    blank["reference_path"] = ref.relative_to(out).as_posix()
    blank["reference_sha256"] = common.sha256_file(ref)
    manifest = write_manifest(out / "samples.json", [blank], library_root=library, query_cases=[
        {"query_id": "q01", "kind": "positive", "sample_id": "syn-blank", "query": "白塔"},
    ])
    prepare.materialize_dataset(manifest)

    run_dir = _run(manifest, tmp_path / "run", fake_worker)
    record = _records(run_dir)[0]
    assert record["status"] == "ok"
    assert record["text"] == ""
    assert record["text_empty"] is True
    assert record["metrics"]["cer"] == 1.0, "空识别必须按整页漏检计入"
    queries = json.loads((run_dir / "queries.json").read_text(encoding="utf-8"))
    assert queries["positives"]["missed"][0]["reason"] == "engine_empty_text"
    assert queries["empty_text_samples"] == ["syn-blank"]


def test_memory_is_null_with_reason_when_psutil_missing(eval_manifest, tmp_path, fake_worker):
    import importlib.util

    out = _run(eval_manifest, tmp_path / "run", fake_worker)
    record = next(r for r in _records(out) if r["sample_id"] == "syn-scan")
    memory = record["memory"]
    assert memory["peak_rss_bytes"] is not None or memory["available"] is False
    if not memory["available"]:
        assert memory["reason"]
        assert memory["peak_rss_bytes"] is None
        assert memory["limit_enforced"] is False
    else:
        assert importlib.util.find_spec("psutil") is not None
        assert memory["peak_rss_bytes"] > 0
        assert memory["limit_bytes"] == common.MAX_CANDIDATE_RSS_BYTES


def test_rss_bound_is_eight_gibibytes(eval_manifest, tmp_path, fake_worker):
    out = _run(eval_manifest, tmp_path / "run", fake_worker)
    assert _run_json(out)["max_rss_bytes"] == 8 * 1024 ** 3
    assert run.exceeds_rss(8 * 1024 ** 3 + 1, 8 * 1024 ** 3) is True
    assert run.exceeds_rss(1024, 8 * 1024 ** 3) is False


def test_params_json_is_child_local_and_fingerprinted(eval_manifest, tmp_path, fake_worker, monkeypatch):
    # Other OCR tests may already have imported the package during collection.
    # Guard THIS operation's imports rather than assuming a pristine shared interpreter.
    import builtins
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "rapidocr" or name.startswith("rapidocr."):
            raise AssertionError("评测父进程不得导入 OCR 引擎")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"Det.thresh": 0.2, "Rec.model_type": "medium"}),
                      encoding="utf-8")

    out = _run(eval_manifest, tmp_path / "run", fake_worker, params_json=params)

    meta = _run_json(out)
    assert meta["config"]["params"] == {"Det.thresh": 0.2, "Rec.model_type": "medium"}
    assert meta["config"]["params_sha256"] == common.sha256_file(params)
    record = next(r for r in _records(out) if r["sample_id"] == "syn-scan")
    assert record["config"]["params"] == {"Det.thresh": 0.2, "Rec.model_type": "medium"}
    plain = _run(eval_manifest, tmp_path / "run-plain", fake_worker)
    assert _run_json(plain)["config"]["params"] is None


def test_run_refuses_output_inside_library(eval_manifest, tmp_path, fake_worker):
    manifest_path = Path(eval_manifest)
    library = manifest_path.parent.parent / "合成库"
    with pytest.raises(common.SafetyError):
        _run(manifest_path, library / "ocr-quality", fake_worker)


# ------------------------------------------------------------- 候选适配器协议


def test_candidate_profile_requires_absolute_python(eval_manifest, tmp_path, fake_candidate):
    run_dir = run.run_benchmark(eval_manifest, tmp_path / "cand", "docling-rapidocr",
                                candidate_python="python",
                                candidate_script=fake_candidate)
    meta = _run_json(run_dir)
    assert meta["status"] == "unavailable"
    assert "绝对" in meta["unavailable_reason"]


def test_candidate_missing_interpreter_is_disabled_and_leaves_baseline_usable(
        eval_manifest, tmp_path, fake_worker):
    missing = tmp_path / "不存在的候选python.exe"
    candidate_dir = run.run_benchmark(eval_manifest, tmp_path / "cand", "docling-rapidocr",
                                      candidate_python=str(missing),
                                      candidate_script=Path("x.py"))
    meta = _run_json(candidate_dir)
    assert meta["status"] == "unavailable"
    assert "不存在" in meta["unavailable_reason"]
    assert not (candidate_dir / "results.jsonl").exists()

    baseline = _run(eval_manifest, tmp_path / "base", fake_worker)
    assert _run_json(baseline)["status"] == "ok", "候选不可用不得影响基线"


def test_candidate_missing_dependency_is_recorded_not_success(tmp_path, synthetic_library,
                                                             fake_candidate):
    library, data = synthetic_library
    out = tmp_path / "eval"
    sample = make_sample(data["scan_pdf"], sample_id="syn-missingdep", doc_id="doc_x",
                         file_page=1)
    manifest = write_manifest(out / "samples.json", [sample], library_root=library)
    prepare.materialize_dataset(manifest)

    run_dir = run.run_benchmark(manifest, tmp_path / "cand", "docling-rapidocr",
                                candidate_python=sys.executable,
                                candidate_script=fake_candidate)
    record = _records(run_dir)[0]
    assert record["status"] == "unavailable"
    assert record["error"]["kind"] == "missing_dependency"
    assert record["text"] is None
    assert record["metrics"]["cer"] is None
    assert _run_json(run_dir)["status"] == "unavailable"


def test_candidate_maps_blocks_and_refuses_to_invent_line_confidence(
        eval_manifest, tmp_path, fake_candidate):
    run_dir = run.run_benchmark(eval_manifest, tmp_path / "cand", "docling-rapidocr",
                                candidate_python=sys.executable,
                                candidate_script=fake_candidate)
    record = next(r for r in _records(run_dir) if r["sample_id"] == "syn-scan")
    assert record["status"] == "ok"
    assert record["granularity"] == "block"
    assert record["coordinate_system"] == "pdf_points_top_left"
    assert record["mean_confidence"] is None
    assert record["confidence_available"] is False
    assert record["confidence_reason"]
    assert record["regions"][0]["bbox"] == [10.0, 20.0, 300.0, 40.0]
    assert record["page"]["width_points"] == 595.0
    assert record["raw_output"]["path"].endswith(".raw.json")
    assert record["runtime_versions"]["docling"] == "0.0.0-fake"
    assert record["synthetic_worker"] is True


def test_docling_adapter_reports_missing_dependency_in_this_env(tmp_path, synthetic_library):
    """真实结果：本机主环境没有 docling，适配器必须报「缺依赖」，不得伪装成功。"""
    library, data = synthetic_library
    out = tmp_path / "eval"
    sample = make_sample(data["scan_pdf"], sample_id="syn-scan", doc_id="doc_x", file_page=1)
    manifest = write_manifest(out / "samples.json", [sample], library_root=library)
    prepare.materialize_dataset(manifest)
    page_pdf = out / json.loads(Path(manifest).read_text(encoding="utf-8"))["samples"][0]["page_pdf"]
    target = tmp_path / "candidate.json"

    completed = subprocess.run(
        [sys.executable, str(common.script_path("docling_adapter.py")),
         "--input", str(page_pdf), "--output", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert completed.returncode == 3
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["error"]["kind"] == "missing_dependency"
    assert "docling" in payload["error"]["message"]
    assert not target.exists(), "缺依赖时不得写出结果文件"


def test_docling_adapter_help_works_without_docling():
    completed = subprocess.run(
        [sys.executable, str(common.script_path("docling_adapter.py")), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert completed.returncode == 0
    assert "--input" in completed.stdout


def test_rapidocr_worker_help_works_without_ocr_packages():
    completed = subprocess.run(
        [sys.executable, str(common.script_path("rapidocr_worker.py")), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert completed.returncode == 0
    assert "--params" in completed.stdout


@pytest.mark.slow
@pytest.mark.skipif("LITWORKBENCH_OCR_QUALITY_SLOW" not in __import__("os").environ,
                    reason="需要真实 RapidOCR 引擎；默认跳过")
def test_real_rapidocr_worker_runs_on_synthetic_page(tmp_path, synthetic_library):
    """真实 RapidOCR 路径（合成页）：记录真实版本、模型文件与哈希。"""
    library, data = synthetic_library
    out = tmp_path / "eval"
    sample = make_sample(data["scan_pdf"], sample_id="syn-scan", doc_id="doc_x", file_page=1)
    manifest = write_manifest(out / "samples.json", [sample], library_root=library)
    prepare.materialize_dataset(manifest)

    run_dir = run.run_benchmark(manifest, tmp_path / "real", "rapidocr-current", repeats=1)
    record = _records(run_dir)[0]
    assert record["status"] == "ok"
    assert record["synthetic_worker"] is False
    assert record["engine_version"], "必须记录真实引擎版本，不能假定 PP-OCR 版本"
    assert record["model_fingerprints"], "必须记录实际模型文件路径与哈希"
    assert any(item["status"] == "ok" for item in record["model_fingerprints"])
    assert all(item["sha256"] for item in record["model_fingerprints"]
               if item["status"] == "ok")
    assert record["granularity"] == "line"
    assert record["mean_confidence"] is not None
    assert record["timing"]["init_seconds"] is not None
    assert record["timing"]["recognize_seconds"][0] > 0
    assert record["production_path"] is True, "无参数时必须走产品生产路径"


def test_metrics_module_stays_free_of_ocr_engines():
    """评分模块不导入任何 OCR 引擎（父进程不因评测而加载引擎）。"""
    assert metrics.score_text("白塔", "白塔")["cer"] == 0.0
    check = subprocess.run([sys.executable, "-B", "-c",
        "import sys; from scripts.ocr_quality import metrics; "
        "assert metrics.score_text('白塔','白塔')['cer']==0; "
        "assert 'rapidocr' not in sys.modules; assert 'onnxruntime' not in sys.modules; "
        "assert 'docling' not in sys.modules"],
        capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


# -------------------------------------------- 调度器与真实子进程的参数契约


@pytest.mark.parametrize("profile, script_name", [("rapidocr-current", "rapidocr_worker.py"),
                                                  ("docling-rapidocr", "docling_adapter.py")])
def test_scheduler_flags_are_accepted_by_the_real_worker_parsers(profile, script_name,
                                                                tmp_path):
    """调度器传的每个参数都必须被子进程自己的 argparse 接受（防标志漂移）。"""
    from scripts.ocr_quality import docling_adapter, rapidocr_worker

    parsers = {"rapidocr_worker.py": rapidocr_worker.build_parser,
               "docling_adapter.py": docling_adapter.build_parser}
    parser = parsers[script_name]()
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    extra = run._worker_args(profile, scale=2.0, repeats=3, init_timeout=600.0,
                             params_json=tmp_path / "params.json")
    command = ["python", str(common.script_path(script_name)),
               "--input", str(tmp_path / "page.pdf"),
               "--output", str(tmp_path / "out.json"), *extra]

    parsed = parser.parse_args(command[2:])  # 不支持的标志会直接 SystemExit
    assert parsed.input.endswith("page.pdf")
    assert parsed.output.endswith("out.json")
    assert parsed.init_timeout == 600.0
    if profile == "rapidocr-current":
        assert parsed.scale == 2.0 and parsed.repeats == 3
        assert str(tmp_path / "params.json") in parsed.params
    else:
        assert parsed.force_ocr is True


def test_real_worker_enforces_init_timeout_with_stub_engine(tmp_path, synthetic_library,
                                                            monkeypatch, capsys):
    """用替身引擎跑真实 worker 入口：超时必须报 init_timeout，而不是继续识别。"""
    from scripts.ocr_quality import prepare, rapidocr_worker
    from litworkbench import ocr as product_ocr

    library, data = synthetic_library
    out = tmp_path / "eval"
    manifest = write_manifest(out / "samples.json", [
        make_sample(data["scan_pdf"], sample_id="syn-x", doc_id="doc_x", file_page=1)
    ], library_root=library)
    prepare.materialize_dataset(manifest)
    page_pdf = out / json.loads(manifest.read_text("utf-8"))["samples"][0]["page_pdf"]

    calls: list[int] = []
    monkeypatch.setattr(product_ocr, "get_engine",
                        lambda: (time.sleep(0.05), calls.append(1))[1])
    monkeypatch.setattr(product_ocr, "recognize",
                        lambda image: {"lines": [], "text": "", "mean_confidence": 0.0})

    target = tmp_path / "result.json"
    code = rapidocr_worker.main(["--input", str(page_pdf), "--output", str(target),
                                 "--repeats", "1", "--init-timeout", "0.001"])

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 4
    assert payload["error"]["kind"] == "init_timeout"
    assert calls == [1], "初始化确实被调用过"
    assert not target.exists(), "超时不得写出结果文件"


def test_real_worker_production_path_writes_result_with_stub_engine(tmp_path, synthetic_library,
                                                                   monkeypatch, capsys):
    """真实入口 + 替身引擎：输出结构与生产 recognize 一致，坐标/置信度如实记录。"""
    from scripts.ocr_quality import prepare, rapidocr_worker
    from litworkbench import ocr as product_ocr

    library, data = synthetic_library
    out = tmp_path / "eval"
    manifest = write_manifest(out / "samples.json", [
        make_sample(data["scan_pdf"], sample_id="syn-x", doc_id="doc_x", file_page=1)
    ], library_root=library)
    prepare.materialize_dataset(manifest)
    page_pdf = out / json.loads(manifest.read_text("utf-8"))["samples"][0]["page_pdf"]

    monkeypatch.setattr(product_ocr, "get_engine", lambda: object())
    monkeypatch.setattr(product_ocr, "recognize", lambda image: {
        "engine": "rapidocr",
        "lines": [{"box": [[0, 0], [1, 0], [1, 1], [0, 1]], "text": "白塔", "confidence": 0.9}],
        "text": "白塔", "mean_confidence": 0.9, "render_width": 10, "render_height": 10})

    target = tmp_path / "result.json"
    code = rapidocr_worker.main(["--input", str(page_pdf), "--output", str(target),
                                 "--repeats", "2", "--init-timeout", "600"])

    capsys.readouterr()
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["production_path"] is True
    assert payload["text"] == "白塔"
    assert payload["granularity"] == "line"
    assert payload["coordinate_system"] == "pixel_top_left"
    assert payload["mean_confidence"] == 0.9
    assert payload["timing"]["init_seconds"] is not None
    assert len(payload["timing"]["recognize_seconds"]) == 2
    assert payload["config"]["params"] is None
    assert payload["model_fingerprints"][0]["status"] == "unknown", \
        "替身引擎没有真实会话时不得编造模型文件"
    assert (tmp_path / "result.json.raw.json").is_file()


def test_real_worker_coerces_params_enums_child_locally(tmp_path, synthetic_library,
                                                       monkeypatch, capsys):
    """``--params`` 的枚举字符串只在本进程内转成 Enum；不带参数时不构造新引擎。"""
    from scripts.ocr_quality import prepare, rapidocr_worker

    library, data = synthetic_library
    out = tmp_path / "eval"
    manifest = write_manifest(out / "samples.json", [
        make_sample(data["scan_pdf"], sample_id="syn-x", doc_id="doc_x", file_page=1)
    ], library_root=library)
    prepare.materialize_dataset(manifest)
    page_pdf = out / json.loads(manifest.read_text("utf-8"))["samples"][0]["page_pdf"]
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"Rec.model_type": "medium", "Det.thresh": 0.2}),
                      encoding="utf-8")

    coerced = rapidocr_worker.coerce_params({"Rec.model_type": "medium", "Det.thresh": 0.2})
    assert repr(coerced["Rec.model_type"]) == "<ModelType.MEDIUM: 'medium'>"
    assert coerced["Det.thresh"] == 0.2

    import rapidocr as rapidocr_pkg

    class FakeEngine:
        def __init__(self, params=None):
            FakeEngine.params = params

        def __call__(self, array):
            return _FakeOutput()

    monkeypatch.setattr(rapidocr_pkg, "RapidOCR", FakeEngine)
    target = tmp_path / "result.json"
    code = rapidocr_worker.main(["--input", str(page_pdf), "--output", str(target),
                                 "--repeats", "1", "--params", str(params)])

    capsys.readouterr()
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["production_path"] is False
    assert payload["config"]["params"] == {"Rec.model_type": "medium", "Det.thresh": 0.2}
    assert payload["config"]["params_sha256"] == common.sha256_file(params)
    assert repr(FakeEngine.params["Rec.model_type"]) == "<ModelType.MEDIUM: 'medium'>"


class _FakeOutput:
    txts = ["白塔"]
    scores = [0.91]
    boxes = [[[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]]]


# ------------------------------------------------------- 内存采样：进程树求和


def test_memory_sampling_sums_live_process_tree_then_peaks():
    """两个子进程时峰值必须是 RSS 之和，不能只取单进程最大值。"""
    from scripts.ocr_quality import workers

    class FakeProcess:
        def __init__(self, rss: int, children: list) -> None:
            self._rss = rss
            self._children = children

        def memory_info(self):
            return SimpleNamespace(rss=self._rss)

        def children(self, recursive: bool = False):
            return self._children

    parent = FakeProcess(100 * 1024 ** 2, [FakeProcess(250 * 1024 ** 2, [])])
    fake_psutil = SimpleNamespace(Process=lambda pid: parent)

    memory = workers.sample_process_tree_memory(1234, 8 * 1024 ** 3,
                                               psutil_module=fake_psutil)

    assert memory["available"] is True
    assert memory["samples"] == 2
    assert memory["peak_rss_bytes"] == 350 * 1024 ** 2, "峰值必须是同一采样点的进程树 RSS 之和"
    assert memory["shared_pages_may_double_count"] is True


def test_memory_sampling_records_missing_psutil_with_reason():
    from scripts.ocr_quality import workers

    memory = workers.sample_process_tree_memory(1234, 8 * 1024 ** 3,
                                               psutil_module=None,
                                               force_missing=True)
    assert memory["available"] is False
    assert memory["peak_rss_bytes"] is None
    assert "psutil" in memory["reason"]


# ---------------------------------------------- 检索用例：漏检必须落盘可见


def test_results_jsonl_persists_missed_query_cases(tmp_path, synthetic_library, fake_worker):
    library, data = synthetic_library
    out = tmp_path / "eval"
    blank = make_sample(data["scan_pdf"], sample_id="syn-blank", doc_id="doc_x", file_page=1)
    ref = write_reference(out / "refs" / "syn-blank.json", sample_id="syn-blank",
                          text="白塔位于县城北侧")
    blank["reference_path"] = ref.relative_to(out).as_posix()
    blank["reference_sha256"] = common.sha256_file(ref)
    manifest = write_manifest(out / "samples.json", [blank], library_root=library, query_cases=[
        {"query_id": "q01", "kind": "positive", "sample_id": "syn-blank", "query": "白塔"},
        {"query_id": "q02", "kind": "negative", "sample_id": "syn-blank", "query": "紫阳书院"},
    ])
    prepare.materialize_dataset(manifest)

    run_dir = _run(manifest, tmp_path / "run", fake_worker)
    record = _records(run_dir)[0]

    cases = {case["query_id"]: case for case in record["query_cases"]}
    assert cases["q01"]["hit"] is False, "漏检的阳性用例必须落在记录里"
    assert cases["q01"]["query"] == "白塔"
    assert cases["q01"]["reason"] == "engine_empty_text"
    assert cases["q02"]["false_hit"] is False
    queries = json.loads((run_dir / "queries.json").read_text(encoding="utf-8"))
    assert queries["positives"]["missed"][0]["query_id"] == "q01"
