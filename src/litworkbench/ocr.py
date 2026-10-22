"""可选的本地 OCR（RapidOCR + ONNX Runtime，CPU）。

约束：

* 引擎**惰性**初始化，每批次只初始化一次；
* 返回真实的行框与置信度，不做任何占位或伪造；
* 未安装依赖时抛出 :class:`OcrUnavailable`，由导入流程把单元标为「待识别」，
  绝不把无文本层的扫描件当成已完成。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from litworkbench.errors import LitError


class OcrUnavailable(LitError):
    """当前环境没有可用的 OCR 引擎。"""


_LOCK = threading.Lock()
_ENGINE: Any | None = None
_IMPORT_ERROR: str | None = None


def _quiet_logs() -> None:
    """RapidOCR 默认会往 stderr 打印 INFO 日志；CLI 的数据走 stdout，这里降噪。"""
    names = ("rapidocr", "onnxruntime", "RapidOCR")
    for name in list(logging.Logger.manager.loggerDict):
        if name.lower().replace(" ", "") not in {n.lower() for n in names}:
            continue
        target = logging.Logger.manager.loggerDict.get(name)
        if isinstance(target, logging.Logger):
            target.setLevel(logging.ERROR)
            target.propagate = False
            for handler in target.handlers:
                handler.setLevel(logging.ERROR)
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.ERROR)
        logger.propagate = False


def _import_engine_class() -> Any:
    global _IMPORT_ERROR
    try:
        from rapidocr import RapidOCR  # type: ignore
    except Exception as exc:  # pragma: no cover - 取决于环境
        _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        raise OcrUnavailable(
            "未安装 OCR 依赖。请执行：uv pip install --python <python> -e '.[ocr]'"
        ) from exc
    return RapidOCR


def is_available() -> bool:
    """只检查依赖，不初始化引擎（doctor / 测试用）。"""
    try:
        _import_engine_class()
    except OcrUnavailable:
        return False
    return True


def availability() -> dict[str, Any]:
    """给 ``lit doctor`` 用的状态描述。"""
    try:
        _import_engine_class()
    except OcrUnavailable as exc:
        return {"available": False, "engine": None, "detail": str(exc)}
    return {
        "available": True,
        "engine": "rapidocr",
        "detail": "RapidOCR + onnxruntime（本地 CPU 推理）",
    }


def get_engine() -> Any:
    """惰性初始化并缓存引擎。"""
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _quiet_logs()
            engine_class = _import_engine_class()
            try:
                _ENGINE = engine_class()
            except Exception as exc:  # pragma: no cover - 取决于环境
                raise OcrUnavailable(f"OCR 引擎初始化失败：{type(exc).__name__}") from exc
            finally:
                _quiet_logs()
        return _ENGINE


def recognize(image: Any) -> dict[str, Any]:
    """识别一张 PIL 图片，返回行框、文本与置信度。

    :param image: ``PIL.Image.Image``
    :returns: ``{'lines': [{'box': [[x, y] * 4], 'text': str, 'confidence': float}],
                'mean_confidence': float, 'render_width': int, 'render_height': int}``
    """
    engine = get_engine()
    width, height = image.size
    try:
        import numpy as np

        array = np.array(image)
    except Exception as exc:  # pragma: no cover - numpy 一定随 rapidocr 安装
        raise OcrUnavailable(f"图像转换失败：{type(exc).__name__}") from exc

    try:
        output = engine(array)
    finally:
        _quiet_logs()

    lines: list[dict[str, Any]] = []
    raw_texts = getattr(output, "txts", None)
    raw_scores = getattr(output, "scores", None)
    raw_boxes = getattr(output, "boxes", None)
    txts = [] if raw_texts is None else list(raw_texts)
    scores = [] if raw_scores is None else list(raw_scores)
    boxes = [] if raw_boxes is None else list(raw_boxes)
    for index, text in enumerate(txts):
        if not str(text).strip():
            continue
        raw_box = boxes[index] if index < len(boxes) else None
        box = [[float(point[0]), float(point[1])] for point in raw_box] if raw_box is not None else []
        score = float(scores[index]) if index < len(scores) else 0.0
        lines.append({"box": box, "text": str(text), "confidence": score})

    mean_confidence = round(sum(line["confidence"] for line in lines) / len(lines), 4) if lines else 0.0
    return {
        "engine": "rapidocr",
        "lines": lines,
        "text": "\n".join(line["text"] for line in lines),
        "mean_confidence": mean_confidence,
        "render_width": int(width),
        "render_height": int(height),
    }


def shutdown() -> None:
    """释放引擎（测试用）。"""
    global _ENGINE
    with _LOCK:
        _ENGINE = None