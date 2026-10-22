"""候选解析器适配器（Docling）：**在候选自己的解释器里**运行。

用法::

    <候选 Python> scripts/ocr_quality/docling_adapter.py \\
        --input <单页 PDF> --output <结果 JSON> [--force-ocr] \\
        [--det-model <ONNX>] [--rec-model <ONNX>] [--cls-model <ONNX>] \\
        [--expect-version <X.Y.Z>]

要点：

* 只用官方接口：``PdfPipelineOptions`` + ``RapidOcrOptions`` + ``DocumentConverter``，
  ``enable_remote_services=False``（县志绝不外发），CPU 推理；
* ``import docling`` 只在真正需要时才发生：没有 docling 时以退出码 3 报“缺依赖”，
  **不是成功**；``--help`` 在没装 docling 的环境也能跑；
* 记录页尺寸、真实块来源（``prov``）与原始文档字典；**没有逐块置信度就写 null**
  并说明原因，绝不伪造行框或置信度；
* 与已验证版本不一致时记录运行期错误，不做静默回退。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_RUNTIME = 4
EXIT_ERROR = 5

ADAPTER_NAME = "docling-rapidocr"


def _emit(payload: dict[str, Any], code: int) -> int:
    # stdout 是给父进程读的协议：只输出 ASCII，避免 Windows 控制台编码影响解析。
    print(json.dumps(payload, ensure_ascii=True))
    return code


def _fail(kind: str, message: str, code: int) -> int:
    return _emit({"error": {"kind": kind, "message": message}}, code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_docling() -> tuple[Any, Any, Any, Any, Any, Any]:
    """只在这里导入候选依赖：主环境不会因为本适配器而安装或加载 docling。"""
    try:
        import docling  # type: ignore
        from docling.datamodel.base_models import InputFormat  # type: ignore
        from docling.datamodel.pipeline_options import (  # type: ignore
            PdfPipelineOptions,
            RapidOcrOptions,
        )
        from docling.document_converter import (  # type: ignore
            DocumentConverter,
            PdfFormatOption,
        )
    except Exception as exc:
        raise ModuleNotFoundError(
            f"候选解释器里没有 docling（{type(exc).__name__}: {exc}）。"
            "请在独立环境安装与锁定版本后重跑；本适配器不会安装任何依赖。"
        ) from exc
    return (docling, InputFormat, PdfPipelineOptions, RapidOcrOptions, DocumentConverter,
            PdfFormatOption)


def require_attribute(target: Any, name: str, *, what: str) -> Any:
    """必须真的设置该属性；接口不支持就报运行期错误，不做静默跳过。"""
    if not hasattr(target, name):
        raise AttributeError(f"{what} 没有 {name}；接口不匹配，记录运行期错误而不是静默回退。")
    return getattr(target, name)


def _ocr_mode_enum() -> Any:
    """找官方 ``OcrMode`` 枚举（新版推荐 ``mode=OcrMode.FULL_PAGE``）。"""
    for module_name in ("docling.datamodel.pipeline_options", "docling.datamodel.base_models"):
        try:
            module = __import__(module_name, fromlist=["OcrMode"])
        except Exception:
            continue
        enum = getattr(module, "OcrMode", None)
        if enum is not None and hasattr(enum, "FULL_PAGE"):
            return enum
    return None


def apply_force_ocr(ocr_options: Any, enabled: bool) -> dict[str, Any]:
    """强制 OCR：优先 ``mode=OcrMode.FULL_PAGE``，否则用仍可用的旧属性。

    用了哪一种机制都会**记录**在 ``config.ocr_options.force_mechanism``，
    不做无声跳过；两者都不存在则报运行期错误。
    """
    enum = _ocr_mode_enum()
    if enum is not None and hasattr(ocr_options, "mode"):
        mode = enum.FULL_PAGE if enabled else enum.FAST if hasattr(enum, "FAST") \
            else enum.FULL_PAGE
        ocr_options.mode = mode
        return {"force_mechanism": "mode=OcrMode.FULL_PAGE",
                "mode": str(getattr(ocr_options.mode, "value", ocr_options.mode))}
    if hasattr(ocr_options, "force_full_page_ocr"):
        ocr_options.force_full_page_ocr = bool(enabled)
        return {"force_mechanism": "force_full_page_ocr（已废弃但可用）",
                "force_full_page_ocr": bool(enabled)}
    raise AttributeError(
        "RapidOcrOptions 既没有 mode 也没有 force_full_page_ocr；接口不匹配，"
        "记录运行期错误而不是静默回退。")


def resolve_ocr_options(RapidOcrOptions: Any, args: argparse.Namespace) -> tuple[Any, dict]:
    """构造官方 ``RapidOcrOptions``：强制 OCR 与中文语言都显式设置。"""
    ocr_options = RapidOcrOptions()
    force_settings = apply_force_ocr(ocr_options, bool(args.force_ocr))
    require_attribute(ocr_options, "lang", what="RapidOcrOptions")
    ocr_options.lang = list(args.lang)
    provided: list[dict[str, str]] = []
    for attribute, path in (("det_model_path", args.det_model),
                            ("rec_model_path", args.rec_model),
                            ("cls_model_path", args.cls_model)):
        if not path:
            continue
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"显式指定的 ONNX 模型不存在：{target}")
        require_attribute(ocr_options, attribute, what="RapidOcrOptions")
        setattr(ocr_options, attribute, str(target))
        provided.append({"parameter": attribute, "path": str(target),
                         "sha256": sha256_file(target)})
    settings = {"force_ocr": bool(args.force_ocr), "lang": list(args.lang),
                "explicit_onnx_models": provided, **force_settings}
    return ocr_options, settings


def package_versions(docling: Any) -> dict[str, str | None]:
    """记录实际装上的版本；候选发行名可能是 ``docling`` 或 ``docling-slim``。"""
    import importlib.metadata as metadata

    versions: dict[str, str | None] = {"python": sys.version.split()[0]}
    for name in ("docling", "docling-slim", "docling-core", "docling-parse",
                 "docling-ibm-models", "rapidocr", "onnxruntime", "pypdfium2"):
        try:
            versions[name] = metadata.version(name)
        except Exception:
            versions[name] = None
    try:
        versions["docling.__version__"] = getattr(docling, "__version__", None)
    except Exception:  # pragma: no cover - 防御性
        versions["docling.__version__"] = None
    return versions


def engine_version_of(versions: dict[str, str | None]) -> str | None:
    return (versions.get("docling.__version__") or versions.get("docling")
            or versions.get("docling-slim"))


def _coord_origin_of(bbox: Any) -> str | None:
    origin = getattr(bbox, "coord_origin", None)
    if origin is None:
        return None
    return str(getattr(origin, "value", origin))


def collect_regions(result: Any) -> tuple[list[dict[str, Any]], str, str | None]:
    """只收 ``result.document`` 里真实存在的 ``prov`` 坐标，并保留坐标来源。"""
    document = getattr(result, "document", result)
    regions: list[dict[str, Any]] = []
    granularity = "none"
    try:
        items = list(document.iterate_items())
    except Exception as exc:
        return [], "none", f"无法遍历文档项：{type(exc).__name__}: {exc}"
    for item, level in items:
        provenance = list(getattr(item, "prov", None) or [])
        text = str(getattr(item, "text", "") or "")
        for prov in provenance:
            bbox = getattr(prov, "bbox", None)
            if bbox is None:
                continue
            if granularity == "none":
                granularity = "block"
            regions.append({
                "bbox": [float(getattr(bbox, name)) for name in ("l", "t", "r", "b")],
                "page": int(getattr(prov, "page_no", 0) or 0),
                "level": level,
                "item_class": type(item).__name__,
                "source": "docling.prov",
                "coord_origin": _coord_origin_of(bbox),
                "text_preview": text[:40],
                "confidence": None,
            })
    return regions, granularity, None


def coordinate_system_of(regions: list[dict[str, Any]]) -> str:
    """按候选实际返回的 ``coord_origin`` 定坐标系，不硬编码左上角。"""
    origins = {str(item.get("coord_origin") or "").upper() for item in regions}
    origins.discard("")
    if not origins:
        return "none"
    if origins == {"TOPLEFT"}:
        return "pdf_points_top_left"
    if origins == {"BOTTOMLEFT"}:
        return "pdf_points_bottom_left"
    return "pdf_points_mixed"


def page_geometry(result: Any) -> dict[str, Any]:
    pages = getattr(getattr(result, "document", result), "pages", None) or {}
    if not pages:
        return {"width_points": None, "height_points": None,
                "reason": "候选结果里没有页尺寸"}
    page = next(iter(pages.values()))
    size = getattr(page, "size", None)
    if size is None:
        return {"width_points": None, "height_points": None, "reason": "页对象没有尺寸"}
    return {
        "width_points": getattr(size, "width", None),
        "height_points": getattr(size, "height", None),
        "page_count": len(pages),
    }


def extract_text(result: Any) -> str:
    """正文导出失败就是失败：不得返回空文本的成功结果。"""
    document = getattr(result, "document", result)
    from docling_core.types.doc import ContentLayer, DocItemLabel
    text = document.export_to_text(
        labels=set(DocItemLabel), included_content_layers=set(ContentLayer), traverse_pictures=True)
    if text is None or not str(text).strip():
        raise RuntimeError("export_to_text 返回空文本")
    return str(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docling_adapter.py",
        description="在候选解释器里运行 Docling（RapidOCR），输出统一结果 JSON。")
    parser.add_argument("--input", required=True, help="单页 PDF")
    parser.add_argument("--output", required=True, help="结果 JSON 路径")
    parser.add_argument("--force-ocr", action="store_true", help="整页强制 OCR")
    parser.add_argument("--det-model", help="显式指定检测 ONNX 路径（可选）")
    parser.add_argument("--rec-model", help="显式指定识别 ONNX 路径（可选）")
    parser.add_argument("--cls-model", help="显式指定方向分类 ONNX 路径（可选）")
    parser.add_argument("--expect-version", help="已验证的 docling 版本；不一致则记运行期错误")
    parser.add_argument("--lang", nargs="+", default=["ch"],
                        help="显式指定 OCR 语言（县志默认 ch）")
    parser.add_argument("--init-timeout", type=float, default=600.0,
                        help="初始化超时秒数（超过即记 init_timeout，不静默继续）")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--layout-manifest", help="固定布局模型版本、文件路径与哈希的本机 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    page_pdf = Path(args.input)
    if not page_pdf.is_file():
        return _fail("usage_error", f"输入文件不存在：{page_pdf}", EXIT_USAGE)
    for destination in (Path(args.output), Path(str(args.output) + ".raw.json")):
        if destination.is_symlink() or (destination.exists() and destination.samefile(page_pdf)):
            return _fail("usage_error", f"输出目标是链接或指向输入原件：{destination}", EXIT_USAGE)

    try:
        (docling, InputFormat, PdfPipelineOptions, RapidOcrOptions, DocumentConverter,
         PdfFormatOption) = import_docling()
    except ModuleNotFoundError as exc:
        return _fail("missing_dependency", str(exc), EXIT_MISSING_DEPENDENCY)

    versions = package_versions(docling)
    actual_version = engine_version_of(versions)
    if args.expect_version and actual_version != args.expect_version:
        return _fail("runtime_error",
                     f"docling 版本与已验证版本不同：实际 {actual_version}，"
                     f"期望 {args.expect_version}；不做静默回退。", EXIT_RUNTIME)

    try:
        import time

        ocr_options, ocr_settings = resolve_ocr_options(RapidOcrOptions, args)
        ocr_options.scale = float(args.scale)
        ocr_settings["scale"] = float(args.scale)
        options = PdfPipelineOptions()
        options.do_ocr = True
        options.do_table_structure = False
        options.enable_remote_services = False
        options.generate_picture_images = False
        options.ocr_options = ocr_options
        layout_fingerprints = []
        layout_manifest = None
        if args.layout_manifest:
            layout_manifest = json.loads(Path(args.layout_manifest).read_text(encoding="utf-8"))
            spec = options.layout_options.model_spec
            if spec.repo_id != layout_manifest["repo"]:
                raise ValueError("布局模型清单与 Docling 模型不匹配")
            spec.revision = layout_manifest["revision"]
            for item in layout_manifest["files"]:
                model_file = Path(layout_manifest["path"]) / item["name"]
                if sha256_file(model_file) != item["sha256"]:
                    raise ValueError(f"布局模型文件哈希变化：{model_file}")
                layout_fingerprints.append({**item, "path": str(model_file), "status": "ok", "role": "layout"})
        accelerator = getattr(options, "accelerator_options", None)
        if accelerator is not None:
            from docling.datamodel.accelerator_options import (  # type: ignore
                AcceleratorDevice,
            )

            require_attribute(accelerator, "device", what="AcceleratorOptions")
            accelerator.device = AcceleratorDevice.CPU
            accelerator.num_threads = 2

        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        started = time.monotonic()
        # 官方绑定：InputFormat.PDF -> PdfFormatOption(pipeline_options=options)
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(
                pipeline_options=options, backend=PyPdfiumDocumentBackend)},
            allowed_formats=[InputFormat.PDF],
        )
        converter.initialize_pipeline(InputFormat.PDF)
        init_seconds = time.monotonic() - started
        if init_seconds > float(args.init_timeout):
            return _fail("init_timeout",
                         f"初始化耗时 {init_seconds:.1f} 秒，超过 {args.init_timeout} 秒上限。",
                         EXIT_RUNTIME)

        convert_seconds = []
        for repeat in range(max(1, int(args.repeats))):
            print(json.dumps({"event": "phase", "phase": "page", "index": repeat}), flush=True)
            started = time.monotonic()
            result = converter.convert(str(page_pdf))
            convert_seconds.append(time.monotonic() - started)
        # 正文导出失败就是失败：不得返回空文本的「成功」。
        text = extract_text(result)
    except FileNotFoundError as exc:
        return _fail("usage_error", str(exc), EXIT_USAGE)
    except AttributeError as exc:
        return _fail("runtime_error", f"Docling 接口不匹配：{exc}", EXIT_RUNTIME)
    except Exception as exc:
        return _fail("runtime_error", f"Docling 运行失败：{type(exc).__name__}: {exc}",
                     EXIT_RUNTIME)

    regions, granularity, region_reason = collect_regions(result)
    geometry = page_geometry(result)
    try:
        document_dict = result.document.export_to_dict()
    except Exception as exc:  # pragma: no cover - 版本差异
        document_dict = None
        export_reason = f"无法导出原始文档字典：{exc}"
    else:
        export_reason = None

    payload = {
        "schema_version": 1,
        "status": "ok",
        "engine": ADAPTER_NAME,
        "engine_version": actual_version,
        "text": text,
        "lines": [],
        "regions": regions,
        "coordinate_system": coordinate_system_of(regions),
        "granularity": granularity,
        "mean_confidence": None,
        "confidence_available": False,
        "confidence_reason": "Docling 的 RapidOcrOptions 不返回逐块置信度；不伪造行置信度",
        "page": geometry,
        "timing": {
            "init_seconds": round(init_seconds, 4),
            "render_seconds": None,
            "recognize_seconds": [round(value, 4) for value in convert_seconds],
            "reason": "候选自行完成版面分析与渲染，渲染与识别耗时不可分",
        },
        "model_fingerprints": ocr_settings["explicit_onnx_models"] + layout_fingerprints,
        "runtime_versions": versions,
        "config": {
            "force_ocr": bool(args.force_ocr),
            "ocr_options": ocr_settings,
            "enable_remote_services": False,
            "allowed_formats": [getattr(InputFormat.PDF, "value", str(InputFormat.PDF))],
            "accelerator": "cpu",
            "num_threads": 2,
            "pdf_backend": "pypdfium2",
            "repeats": max(1, int(args.repeats)),
            "layout_model": ({"repo": layout_manifest["repo"], "revision": layout_manifest["revision"]}
                             if layout_manifest else {"status": "unfingerprinted"}),
            "text_export": "all labels and content layers; traverse picture text",
            "expected_version": args.expect_version,
        },
        "raw_output": {"document_dict": document_dict},
        "notes": [item for item in (region_reason, export_reason) if item],
    }

    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_path = Path(str(target) + ".raw.json")
    raw_path.write_text(json.dumps(document_dict, ensure_ascii=False), encoding="utf-8")
    payload["raw_output"] = {"path": raw_path.name, "sha256": sha256_file(raw_path)}
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return _emit({"ok": True, "output": str(target), "raw_output": str(raw_path),
                  "engine_version": actual_version,
                  "regions": len(regions), "granularity": granularity}, EXIT_OK)


if __name__ == "__main__":  # pragma: no cover - 候选环境入口
    raise SystemExit(main())
