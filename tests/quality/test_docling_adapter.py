"""子进程协议：官方 Docling 绑定、坐标来源、版本元数据、文本导出失败。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_quality import common
from scripts.ocr_quality import docling_adapter as adapter

ADAPTER = str(common.script_path("docling_adapter.py"))

FAKE_DOCLING_VERSION = "2.139.0"

FILES = {
    "docling_core/__init__.py": "",
    "docling_core/types/__init__.py": "",
    "docling_core/types/doc/__init__.py": "from enum import Enum\nclass ContentLayer(str,Enum):\n BODY='body'\n FURNITURE='furniture'\nclass DocItemLabel(str,Enum):\n TEXT='text'\n PAGE_HEADER='page_header'\n",
    "docling/backend/__init__.py": "",
    "docling/backend/pypdfium2_backend.py": "class PyPdfiumDocumentBackend: pass\n",
    "docling/__init__.py": '__version__ = "2.139.0"\n',
    "docling/datamodel/__init__.py": "",
    "docling/datamodel/base_models.py": (
        "from enum import Enum\n\n\nclass InputFormat(str, Enum):\n"
        '    PDF = "pdf"\n'
    ),
    "docling/datamodel/accelerator_options.py": (
        "from dataclasses import dataclass\nfrom enum import Enum\n\n\n"
        "class AcceleratorDevice(str, Enum):\n    CPU = 'cpu'\n    CUDA = 'cuda'\n\n\n"
        "@dataclass\nclass AcceleratorOptions:\n    device: AcceleratorDevice = "
        "AcceleratorDevice.CUDA\n"
    ),
    "docling/datamodel/pipeline_options.py": (
        "from dataclasses import dataclass, field\n\n\n"
        "@dataclass\nclass RapidOcrOptions:\n    force_full_page_ocr: bool = False\n"
        "    lang: list = field(default_factory=lambda: ['ch'])\n"
        "    det_model_path: str = ''\n    rec_model_path: str = ''\n"
        "    cls_model_path: str = ''\n\n\n"
        "@dataclass\nclass PdfPipelineOptions:\n    do_ocr: bool = True\n"
        "    do_table_structure: bool = True\n    generate_picture_images: bool = True\n"
        "    enable_remote_services: bool = False\n    accelerator_options: object = None\n"
        "    ocr_options: object = None\n\n    def __post_init__(self):\n"
        "        if self.accelerator_options is None:\n"
        "            from .accelerator_options import AcceleratorOptions\n"
        "            self.accelerator_options = AcceleratorOptions()\n"
    ),
    "docling/document_converter/__init__.py": '''
import os
from dataclasses import asdict, is_dataclass
from types import SimpleNamespace


class PdfFormatOption:
    def __init__(self, pipeline_options=None, backend=None):
        if backend is None:
            raise ValueError("Explicit PDFium backend required")
        self.pipeline_options = pipeline_options


class _BBox:
    def __init__(self):
        self.l, self.t, self.r, self.b = 10.0, 20.0, 300.0, 40.0
        self.coord_origin = "BOTTOMLEFT"


class _Prov:
    def __init__(self):
        self.bbox = _BBox()
        self.page_no = 1


class _Item:
    def __init__(self, text, prov):
        self.text = text
        self.prov = [prov]


class FakeDocument:
    def __init__(self, options):
        self._pages = {"1": SimpleNamespace(size=SimpleNamespace(width=595.0, height=842.0))}
        self.received = {
            "pipeline_options": asdict(options) if is_dataclass(options) else vars(options),
            "ocr_options": asdict(options.ocr_options),
            "accelerator_device": str(options.accelerator_options.device),
        }

    @property
    def pages(self):
        return self._pages

    def iterate_items(self):
        return [(_Item("白塔位于县城北侧", _Prov()), 1)]

    def export_to_text(self, **kwargs):
        assert kwargs.get("traverse_pictures") is True
        assert "furniture" in kwargs["included_content_layers"]
        assert "page_header" in kwargs["labels"]
        if os.environ.get("FAKE_DOCLING_TEXT_FAILS"):
            raise RuntimeError("export_to_text 不可用")
        return "白塔位于县城北侧"

    def export_to_dict(self):
        return {"fake": True, "received": self.received}


class ConversionResult:
    """故意**不**提供 iterate_items/pages：适配器必须走 result.document。"""

    def __init__(self, document):
        self.document = document
        self.pages = {}
        self.input = None


class DocumentConverter:
    def __init__(self, format_options=None, allowed_formats=None):
        if not isinstance(format_options, dict) or list(format_options) != ["pdf"]:
            raise TypeError("format_options 必须以 InputFormat.PDF 为键")
        option = format_options["pdf"]
        if not isinstance(option, PdfFormatOption) or option.pipeline_options is None:
            raise TypeError("必须使用 PdfFormatOption(pipeline_options=options)")
        if not allowed_formats or [getattr(f, "value", f) for f in allowed_formats] != ["pdf"]:
            raise ValueError("必须显式声明 allowed_formats=[InputFormat.PDF]")
        self.options = option.pipeline_options

    def initialize_pipeline(self, format):
        self.initialized = True

    def convert(self, path):
        assert self.initialized, "Model initialization belongs in init timing"
        return ConversionResult(FakeDocument(self.options))
''',
    "docling_slim-2.139.0.dist-info/METADATA": (
        "Metadata-Version: 2.1\nName: docling-slim\nVersion: 2.139.0\n"
    ),
}


@pytest.fixture
def candidate_env(tmp_path) -> Path:
    """一个假的候选环境：官方 API 形状 + ``docling-slim`` 发行元数据。"""
    root = tmp_path / "candidate-env"
    for name, content in FILES.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _run_adapter(candidate_env: Path, page_pdf: Path, output: Path,
                 extra: list[str] | None = None, env: dict | None = None):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(candidate_env)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, ADAPTER, "--input", str(page_pdf), "--output", str(output),
         *(extra or [])],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=environment,
    )


@pytest.fixture
def page_pdf(tmp_path, synthetic_library) -> Path:
    from scripts.ocr_quality import prepare
    from tests.quality.conftest import make_sample, write_manifest

    library, data = synthetic_library
    out = tmp_path / "eval"
    sample = make_sample(data["scan_pdf"], sample_id="syn-scan", doc_id="doc_x", file_page=1)
    manifest = write_manifest(out / "samples.json", [sample], library_root=library)
    prepare.materialize_dataset(manifest)
    entry = json.loads(manifest.read_text(encoding="utf-8"))["samples"][0]
    return out / entry["page_pdf"]


def test_adapter_uses_official_docling_api_and_captures_slim_version(candidate_env, page_pdf,
                                                                     tmp_path):
    output = tmp_path / "candidate.json"
    completed = _run_adapter(candidate_env, page_pdf, output, ["--force-ocr"])

    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["engine_version"] == FAKE_DOCLING_VERSION
    assert payload["runtime_versions"]["docling-slim"] == FAKE_DOCLING_VERSION, \
        "候选发行名是 docling-slim，不能只报 None"
    assert payload["runtime_versions"]["docling"] is None

    received = json.loads((tmp_path / "candidate.json.raw.json").read_text("utf-8"))["received"]
    assert received["pipeline_options"]["enable_remote_services"] is False
    assert received["pipeline_options"]["do_ocr"] is True
    assert received["accelerator_device"] in {"AcceleratorDevice.CPU", "cpu"}
    assert received["ocr_options"]["force_full_page_ocr"] is True, \
        "强制 OCR 必须落在 RapidOcrOptions 上"
    assert received["ocr_options"]["lang"] == ["ch"], "中文识别语言必须显式指定"


def test_adapter_preserves_actual_coord_origin_and_no_fake_confidence(candidate_env, page_pdf,
                                                                     tmp_path):
    output = tmp_path / "candidate.json"
    assert _run_adapter(candidate_env, page_pdf, output).returncode == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["coordinate_system"] == "pdf_points_bottom_left", \
        "坐标来源由候选实际返回决定，不能硬编码左上角"
    assert payload["granularity"] == "block"
    assert payload["regions"][0]["coord_origin"] == "BOTTOMLEFT"
    assert payload["regions"][0]["confidence"] is None
    assert payload["mean_confidence"] is None
    assert payload["confidence_available"] is False
    assert payload["confidence_reason"]
    assert payload["page"]["width_points"] == 595.0
    assert payload["text"] == "白塔位于县城北侧"


def test_text_export_failure_is_not_reported_as_ok(candidate_env, page_pdf, tmp_path):
    output = tmp_path / "candidate.json"
    completed = _run_adapter(candidate_env, page_pdf, output,
                             env={"FAKE_DOCLING_TEXT_FAILS": "1"})
    assert completed.returncode == 4
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["error"]["kind"] == "runtime_error"
    assert "export_to_text" in payload["error"]["message"]
    assert not output.exists(), "文本导出失败不得写出空文本的成功结果"


def test_version_mismatch_is_recorded_not_silently_accepted(candidate_env, page_pdf, tmp_path):
    completed = _run_adapter(candidate_env, page_pdf, tmp_path / "candidate.json",
                             ["--expect-version", "2.100.0"])
    assert completed.returncode == 4
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "不做静默回退" in payload["error"]["message"]


# --------------------------------------------------------------- 纯单元检查


def test_collect_regions_uses_document_api_and_records_coord_origin():
    result = SimpleNamespace(document=SimpleNamespace(
        iterate_items=lambda: [(_Region("塔", (1.0, 2.0, 3.0, 4.0), 2, "TOPLEFT"), 1)]))
    with pytest.raises(AttributeError):
        result.iterate_items  # 结果对象本身没有该方法：必须走 document

    regions, granularity, reason = adapter.collect_regions(result)
    assert reason is None
    assert granularity == "block"
    assert regions[0]["page"] == 2
    assert regions[0]["bbox"] == [1.0, 2.0, 3.0, 4.0]
    assert regions[0]["coord_origin"] == "TOPLEFT"
    assert regions[0]["confidence"] is None


def test_page_geometry_uses_document_pages():
    result = SimpleNamespace(document=SimpleNamespace(
        pages={"1": SimpleNamespace(size=SimpleNamespace(width=595, height=842))}))
    assert adapter.page_geometry(result)["width_points"] == 595
    assert adapter.page_geometry(SimpleNamespace(document=SimpleNamespace(pages={})))["reason"]


def test_package_versions_includes_docling_slim():
    versions = adapter.package_versions(SimpleNamespace(__version__=FAKE_DOCLING_VERSION))
    assert "docling-slim" in versions
    assert versions["docling-slim"] is None or isinstance(versions["docling-slim"], str)


def test_adapter_help_runs_in_candidate_env(candidate_env, tmp_path):
    completed = subprocess.run([sys.executable, ADAPTER, "--help"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120,
                               env={**os.environ, "PYTHONPATH": str(candidate_env)})
    assert completed.returncode == 0
    assert "--force-ocr" in completed.stdout


class _Region:
    def __init__(self, text, bbox, page_no, coord_origin):
        self.text = text
        self.prov = [SimpleNamespace(
            page_no=page_no,
            bbox=SimpleNamespace(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3],
                                 coord_origin=coord_origin))]
