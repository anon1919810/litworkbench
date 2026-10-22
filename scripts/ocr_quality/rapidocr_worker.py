"""RapidOCR 基线**子进程**：一页 PDF → 结果 JSON。

两种模式，输出结构完全一致：

* **不带 ``--params``**：直接调用 ``litworkbench.ocr.recognize(image)``，
  即当前产品的生产路径（同一引擎初始化、同一行框与置信度）；
* **带 ``--params``**：在**本子进程内**用 ``RapidOCR(params=...)`` 构造引擎，
  用于比较 PP-OCRv6 medium、检测阈值等；不改产品源码、不改主环境、不写全局配置。

无论哪种模式，都记录**实际**加载的模型文件路径与 SHA-256、实际生效的配置与
运行版本；读不到就写 ``unknown`` + 原因，绝不假定 PP-OCR 版本。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_RUNTIME = 4
EXIT_ERROR = 5

ENUM_PARAMS = {"engine_type", "model_type", "ocr_version", "task_type"}
COMPONENTS = ("Det", "Rec", "Cls")
COMPONENT_ATTRS = {"Det": "text_det", "Rec": "text_rec", "Cls": "text_cls"}


def _emit(payload: dict[str, Any], code: int) -> int:
    # stdout 是给父进程读的协议：只输出 ASCII，避免 Windows 控制台编码影响解析。
    print(json.dumps(payload, ensure_ascii=True))
    return code


def _fail(kind: str, message: str, code: int) -> int:
    return _emit({"error": {"kind": kind, "message": message}}, code)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def coerce_params(raw: dict[str, Any]) -> dict[str, Any]:
    """把 JSON 里的字符串枚举转成 RapidOCR 要求的 Enum（仅在本子进程内生效）。"""
    from rapidocr.utils import typings

    classes = {
        "engine_type": typings.EngineType,
        "model_type": typings.ModelType,
        "ocr_version": typings.OCRVersion,
        "task_type": typings.TaskType,
    }
    coerced: dict[str, Any] = {}
    for key, value in raw.items():
        enum = classes.get(str(key).split(".")[-1])
        if enum is None:
            coerced[key] = value
        elif isinstance(value, enum):
            coerced[key] = value
        else:
            try:
                coerced[key] = enum(str(value))
            except ValueError:
                members = {member.name.lower(): member for member in enum}
                coerced[key] = members[str(value).lower()]
    return coerced


@contextmanager
def capture_model_paths(recorded: list[str]) -> Iterator[None]:
    """记录**实际**构造 ONNX 会话时用到的模型文件（子进程内的临时包装）。"""
    import onnxruntime

    targets = [onnxruntime]
    try:
        from rapidocr.inference_engine.onnxruntime import main as ort_main

        if hasattr(ort_main, "InferenceSession"):
            targets.append(ort_main)
    except Exception:  # pragma: no cover - 结构变化时按未知处理
        pass
    originals = [(module, module.InferenceSession) for module in targets]
    original = onnxruntime.InferenceSession

    class Recording(original):  # type: ignore[misc,valid-type]
        def __init__(self, path_or_bytes, *args: Any, **kwargs: Any) -> None:
            if isinstance(path_or_bytes, (str, Path)):
                recorded.append(str(path_or_bytes))
            super().__init__(path_or_bytes, *args, **kwargs)

    try:
        for module in targets:
            module.InferenceSession = Recording
        yield
    finally:
        for module, value in originals:
            module.InferenceSession = value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def config_snapshot(engine: Any) -> dict[str, Any]:
    """记录实际生效的配置（不假定版本：值全部来自引擎自己的 cfg）。"""
    cfg = getattr(engine, "cfg", None)
    if cfg is None:
        return {}
    try:
        from omegaconf import OmegaConf

        container = OmegaConf.to_container(cfg, resolve=True)
    except Exception:  # pragma: no cover - 结构变化时按未知处理
        return {"reason": f"无法序列化引擎配置：{type(cfg).__name__}"}
    snapshot: dict[str, Any] = {}
    for section in COMPONENTS + ("Global",):
        value = container.get(section) if isinstance(container, dict) else None
        if not isinstance(value, dict):
            continue
        keys = ("model_path", "model_dir", "model_type", "ocr_version", "engine_type",
                "lang_type", "task_type")
        snapshot[section] = {key: _jsonable(value[key]) for key in keys if key in value}
        if section != "Global":
            snapshot[section]["thresh"] = _jsonable(value.get("thresh"))
            snapshot[section]["box_thresh"] = _jsonable(value.get("box_thresh"))
            snapshot[section]["limit_side_len"] = _jsonable(value.get("limit_side_len"))
            snapshot[section]["limit_type"] = _jsonable(value.get("limit_type"))
        else:
            snapshot[section]["text_score"] = _jsonable(value.get("text_score"))
    return snapshot


def model_fingerprints(recorded: list[str], engine: Any) -> list[dict[str, Any]]:
    """实际加载的模型文件：路径 + SHA-256；读不到就写 unknown + 原因。"""
    if not recorded:
        return [{"index": 0, "path": None, "sha256": None, "status": "unknown",
                 "reason": "未捕获到 ONNX 会话构造：可能未执行识别，或推理引擎不是 onnxruntime"}]
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(recorded):
        path = Path(raw)
        exists = path.is_file()
        entries.append({
            "index": index,
            "path": str(path),
            "filename": path.name,
            "sha256": sha256_file(path) if exists else None,
            "size_bytes": path.stat().st_size if exists else None,
            "status": "ok" if exists else "missing",
            "reason": None if exists else "进程记录到的模型路径在文件系统上不存在",
        })
    return entries


def shape_output(raw: Any) -> dict[str, Any]:
    """与 ``litworkbench.ocr.recognize`` 完全一致的输出结构。"""
    lines: list[dict[str, Any]] = []
    texts = getattr(raw, "txts", None)
    scores = getattr(raw, "scores", None)
    boxes = getattr(raw, "boxes", None)
    texts = [] if texts is None else list(texts)
    scores = [] if scores is None else list(scores)
    boxes = [] if boxes is None else list(boxes)
    for index, text in enumerate(texts):
        if not str(text).strip():
            continue
        box = boxes[index] if index < len(boxes) else None
        coordinates = [[float(point[0]), float(point[1])] for point in box] if box is not None else []
        score = float(scores[index]) if index < len(scores) else 0.0
        lines.append({"box": coordinates, "text": str(text), "confidence": score})
    mean = round(sum(line["confidence"] for line in lines) / len(lines), 4) if lines else 0.0
    return {
        "engine": "rapidocr",
        "lines": lines,
        "text": "\n".join(line["text"] for line in lines),
        "mean_confidence": mean,
    }


def render_page(page_pdf: Path, scale: float):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(page_pdf))
    try:
        page = pdf[0]
        try:
            return page.render(scale=float(scale)).to_pil().convert("RGB"), {
                "width_points": float(page.get_width()), "height_points": float(page.get_height()),
            }
        finally:
            page.close()
    finally:
        pdf.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rapidocr_worker.py",
        description="RapidOCR 基线子进程：读单页 PDF，写统一结果 JSON。")
    parser.add_argument("--input", required=True, help="单页 PDF（派生页，永远只有第 1 页）")
    parser.add_argument("--output", required=True, help="结果 JSON 路径")
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--repeats", type=int, default=3, help="温运行次数（>=1）")
    parser.add_argument("--params", help="RapidOCR 参数 JSON（仅本子进程生效）")
    parser.add_argument("--init-timeout", type=float, default=600.0,
                        help="引擎初始化超时秒数（超过即记 init_timeout，不静默继续）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    page_pdf = Path(args.input)
    if not page_pdf.is_file():
        return _fail("usage_error", f"输入文件不存在：{page_pdf}", EXIT_USAGE)
    for destination in (Path(args.output), Path(str(args.output) + ".raw.json")):
        if destination.is_symlink() or (destination.exists() and destination.samefile(page_pdf)):
            return _fail("usage_error", f"输出目标是链接或指向输入原件：{destination}", EXIT_USAGE)

    params_raw: dict[str, Any] = {}
    params_sha = None
    if args.params:
        params_path = Path(args.params)
        if not params_path.is_file():
            return _fail("usage_error", f"参数文件不存在：{params_path}", EXIT_USAGE)
        try:
            params_raw = json.loads(params_path.read_text(encoding="utf-8"))
            params_sha = sha256_file(params_path)
        except Exception as exc:
            return _fail("usage_error", f"参数文件无法解析：{exc}", EXIT_USAGE)
        if not isinstance(params_raw, dict):
            return _fail("usage_error", "参数文件必须是 JSON 对象", EXIT_USAGE)

    try:
        import numpy  # noqa: F401
        import pypdfium2  # noqa: F401
    except Exception as exc:
        return _fail("missing_dependency", f"缺少渲染依赖：{exc}", EXIT_MISSING_DEPENDENCY)

    import importlib.metadata as metadata

    versions: dict[str, Any] = {"python": sys.version.split()[0]}
    for name in ("rapidocr", "onnxruntime", "pypdfium2", "litworkbench"):
        try:
            versions[name] = metadata.version(name)
        except Exception:
            versions[name] = None

    recorded: list[str] = []
    engine = None
    init_seconds: float | None = None
    render_seconds: list[float] = []
    recognize_seconds: list[float] = []
    last: dict[str, Any] | None = None
    page_size: dict[str, Any] = {}
    image_size: tuple[int, int] | None = None
    production_path = False

    try:
        with capture_model_paths(recorded):
            if params_raw:
                try:
                    from rapidocr import RapidOCR
                except Exception as exc:
                    return _fail("missing_dependency", f"未安装 rapidocr：{exc}",
                                 EXIT_MISSING_DEPENDENCY)
                started = time.monotonic()
                try:
                    engine = RapidOCR(params=coerce_params(params_raw))
                except Exception as exc:
                    return _fail("runtime_error",
                                 f"RapidOCR(params=...) 初始化失败（参数或模型不可用）："
                                 f"{type(exc).__name__}: {exc}", EXIT_RUNTIME)
                init_seconds = time.monotonic() - started
                if init_seconds > float(args.init_timeout):
                    return _fail("init_timeout",
                                 f"引擎初始化耗时 {init_seconds:.1f} 秒，超过 "
                                 f"{args.init_timeout} 秒上限。", EXIT_RUNTIME)
            else:
                try:
                    from litworkbench import ocr as product_ocr
                except Exception as exc:
                    return _fail("missing_dependency", f"无法导入 litworkbench：{exc}",
                                 EXIT_MISSING_DEPENDENCY)
                production_path = True
                started = time.monotonic()
                try:
                    engine = product_ocr.get_engine()
                except Exception as exc:
                    return _fail("missing_dependency",
                                 f"当前环境没有可用 OCR 引擎：{type(exc).__name__}: {exc}",
                                 EXIT_MISSING_DEPENDENCY)
                init_seconds = time.monotonic() - started
                if init_seconds > float(args.init_timeout):
                    return _fail("init_timeout",
                                 f"OCR 引擎初始化耗时 {init_seconds:.1f} 秒，超过 "
                                 f"{args.init_timeout} 秒上限。", EXIT_RUNTIME)

            for _repeat in range(max(1, int(args.repeats))):
                print(json.dumps({"event": "phase", "phase": "page", "index": _repeat}), flush=True)
                started = time.monotonic()
                try:
                    image, page_size = render_page(page_pdf, args.scale)
                except Exception as exc:
                    return _fail("runtime_error", f"渲染失败：{type(exc).__name__}: {exc}",
                                 EXIT_RUNTIME)
                render_seconds.append(time.monotonic() - started)
                image_size = image.size
                try:
                    if production_path:
                        started = time.monotonic()
                        last = product_ocr.recognize(image)
                        recognize_seconds.append(time.monotonic() - started)
                    else:
                        import numpy as np

                        array = np.array(image)
                        started = time.monotonic()
                        last = shape_output(engine(array))
                        recognize_seconds.append(time.monotonic() - started)
                except Exception as exc:
                    return _fail("runtime_error", f"识别失败：{type(exc).__name__}: {exc}",
                                 EXIT_RUNTIME)
                finally:
                    image.close()
    except Exception as exc:  # pragma: no cover - 兜底
        return _fail("error", f"子进程异常：{type(exc).__name__}: {exc}", EXIT_ERROR)

    assert last is not None

    lines = last.get("lines") or []
    regions = [{"bbox": line["box"], "page": 1, "source": "rapidocr.line",
                "confidence": line.get("confidence")} for line in lines if line.get("box")]
    payload = {
        "schema_version": 1,
        "status": "ok",
        "engine": "rapidocr",
        "engine_version": versions.get("rapidocr"),
        "production_path": production_path,
        "text": last.get("text") or "",
        "lines": lines,
        "regions": regions,
        "coordinate_system": "pixel_top_left" if regions else "none",
        "granularity": "line" if regions else "none",
        "mean_confidence": last.get("mean_confidence"),
        "confidence_available": bool(lines),
        "confidence_reason": None if lines else "引擎没有返回任何行：没有真实置信度，写 null",
        "page": {**(page_size or {}),
                 "render_width": image_size[0] if image_size else None,
                 "render_height": image_size[1] if image_size else None},
        "timing": {
            "init_seconds": round(init_seconds, 4) if init_seconds is not None else None,
            "render_seconds": [round(item, 4) for item in render_seconds],
            "recognize_seconds": [round(item, 4) for item in recognize_seconds],
        },
        "model_fingerprints": model_fingerprints(recorded, engine),
        "runtime_versions": versions,
        "config": {
            "scale": float(args.scale),
            "repeats": int(args.repeats),
            "params": params_raw or None,
            "params_sha256": params_sha,
            "production_path": production_path,
            "effective": config_snapshot(engine),
        },
        "raw_output": {"lines": lines},
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_path = Path(str(target) + ".raw.json")
    raw_path.write_text(json.dumps({"lines": lines, "model_fingerprints":
                                    payload["model_fingerprints"]}, ensure_ascii=False),
                        encoding="utf-8")
    payload["raw_output"] = {"path": raw_path.name, "sha256": sha256_file(raw_path),
                             "lines": None}
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return _emit({"ok": True, "output": str(target), "raw_output": str(raw_path),
                  "engine_version": versions.get("rapidocr"),
                  "model_fingerprints": payload["model_fingerprints"]}, EXIT_OK)


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    raise SystemExit(main())
