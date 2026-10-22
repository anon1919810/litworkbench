"""固定识别配置：两种原始版面配置 + 真实本机能力探测。

只提供两个配置（冻结契约）：

* ``compatible-v1``    保留当前产品默认识别行为；
* ``small-lowmem-v1``  省资源：渲染 2 倍、识字/方向分类批次 1、ONNX 线程 2、
  初始化 120 秒、单页 60 秒、RSS/私有提交上限 2.5 GiB。

约束：

* :func:`get_profiles` 只回显 ``{id, label, description, available, reason}``，
  **绝不**包含任何本机绝对模型路径；
* 能力探测只读取本机已安装依赖/权重状态，**绝不**初始化重型引擎、绝不访问网络、
  绝不自动下载模型；
* :func:`profile_runtime` 返回的具体参数会被送进受控子进程。
"""

from __future__ import annotations

from typing import Any
from pathlib import Path
from importlib.util import find_spec

from litworkbench.errors import ValidationError

PROFILE_COMPATIBLE = "compatible-v1"
PROFILE_SMALL_LOWMEM = "small-lowmem-v1"
PROFILES = (PROFILE_COMPATIBLE, PROFILE_SMALL_LOWMEM)

#: 2.5 GiB，RSS 与私有提交各自的上限。
MAX_MEMORY_BYTES = 2684354560
INIT_TIMEOUT_SECONDS = 120
PAGE_TIMEOUT_SECONDS = 60

_MISSING_REASON = "本机缺少推理依赖或模型权重"

#: 各配置的固定运行参数（不得被 UI/请求覆盖）。
_LOWMEM_RUNTIME: dict[str, Any] = {
    "render_scale": 2,
    "det_batch": 1,
    "rec_batch": 1,
    "cls_batch": 1,
    "onnx_intra_threads": 2,
    "onnx_inter_threads": 2,
    "init_timeout": INIT_TIMEOUT_SECONDS,
    "page_timeout": PAGE_TIMEOUT_SECONDS,
    "max_memory_bytes": MAX_MEMORY_BYTES,
}

#: ``compatible-v1`` 保留产品默认行为，不被强制成省资源批次。
_COMPATIBLE_RUNTIME: dict[str, Any] = {
    "render_scale": 2,
    "det_batch": 1,
    "rec_batch": 6,
    "cls_batch": 6,
    "onnx_intra_threads": -1,
    "onnx_inter_threads": -1,
    "init_timeout": INIT_TIMEOUT_SECONDS,
    "page_timeout": PAGE_TIMEOUT_SECONDS,
    "max_memory_bytes": MAX_MEMORY_BYTES,
}

_LABELS = {
    PROFILE_COMPATIBLE: ("兼容配置", "保留当前默认识别行为，逐页处理。"),
    PROFILE_SMALL_LOWMEM: ("省资源配置", "逐页处理并减小单次识字批次，降低峰值内存。"),
}


def _local_capability() -> dict[str, Any]:
    """探测本机真实的推理依赖/权重可用性（窄注入接缝，供测试替换）。

    返回 ``{"available": bool, "reason": str | None}``：缺少依赖或权重时返回
    ``available=False`` 与显式中文原因；绝不静默、绝不假装可用，也绝不联网下载。
    """
    try:
        if any(find_spec(name) is None for name in
               ("rapidocr", "onnxruntime", "pypdfium2", "numpy", "psutil")):
            return {"available": False, "reason": _MISSING_REASON}
        model_paths()
        import psutil
        info = psutil.Process().memory_info()
        if info.rss <= 0 or getattr(info, "private", None) is None:
            return {"available": False, "reason": "本机缺少可用的 RSS/私有内存监控"}
    except Exception:
        return {"available": False, "reason": _MISSING_REASON}
    return {"available": True, "reason": None}


def model_paths() -> dict[str, str]:
    """Only existing packaged weights; passing explicit paths prevents download fallback."""
    spec = find_spec("rapidocr")
    if spec is None or not spec.origin:
        raise ValidationError(_MISSING_REASON)
    folder = Path(spec.origin).parent / "models"
    names = {"Det": "PP-OCRv6_det_small.onnx", "Rec": "PP-OCRv6_rec_small.onnx",
             "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx"}
    paths = {key: (folder / name).resolve() for key, name in names.items()}
    if any(not path.is_file() or path.stat().st_size == 0 for path in paths.values()):
        raise ValidationError(_MISSING_REASON)
    return {key: str(path) for key, path in paths.items()}


def get_profiles() -> list[dict[str, Any]]:
    """返回两个固定配置的可选清单（无本机路径、无网络访问）。"""
    capability = _local_capability() or {"available": False, "reason": _MISSING_REASON}
    available = bool(capability.get("available"))
    reason = capability.get("reason")
    if available:
        reason = None
    elif not reason:
        reason = _MISSING_REASON
    items: list[dict[str, Any]] = []
    for profile_id in PROFILES:
        label, description = _LABELS[profile_id]
        items.append(
            {
                "id": profile_id,
                "label": label,
                "description": description,
                "available": available,
                "reason": reason,
            }
        )
    return items


def profile_runtime(profile_id: str) -> dict[str, Any]:
    """返回某配置的固定运行参数；未知配置抛 :class:`ValidationError`。"""
    if profile_id == PROFILE_SMALL_LOWMEM:
        return dict(_LOWMEM_RUNTIME)
    if profile_id == PROFILE_COMPATIBLE:
        return dict(_COMPATIBLE_RUNTIME)
    raise ValidationError(
        f"未知的识别配置：{profile_id}（可选 {'/'.join(PROFILES)}）"
    )


__all__ = [
    "PROFILE_COMPATIBLE",
    "PROFILE_SMALL_LOWMEM",
    "PROFILES",
    "MAX_MEMORY_BYTES",
    "get_profiles",
    "profile_runtime",
]
