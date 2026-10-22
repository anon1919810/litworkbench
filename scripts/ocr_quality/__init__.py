"""县志 OCR 质量评测工具（内部验收用，不是产品功能入口）。

模块分工：

* :mod:`prepare` —— 只读清点、派生单页 PDF/PNG、清单校验；
* :mod:`metrics` —— 字符编辑距离、检索用例核对与合并口径（不加载任何引擎）；
* :mod:`run` —— 受控子进程调度（RapidOCR 基线子进程 / 候选适配器）；
* :mod:`report` —— 本地 HTML/JSON/CSV 对照报告。

只读边界、参考稿核稿级别与「缺失即 None」的口径统一放在 :mod:`common`。
"""

from __future__ import annotations

from .common import (  # noqa: F401
    RECORD_VERSION,
    SCHEMA_VERSION,
    EvaluationError,
    SafetyError,
    ValidationFailure,
)

__all__ = [
    "EvaluationError",
    "RECORD_VERSION",
    "SCHEMA_VERSION",
    "SafetyError",
    "ValidationFailure",
]