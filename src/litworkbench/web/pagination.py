"""契约里的统一分页：``{items, total, page, page_size}``，默认 30、最大 100。"""

from __future__ import annotations

from typing import Any

from litworkbench.errors import ValidationError

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


def normalize_page(page: Any, page_size: Any) -> tuple[int, int]:
    try:
        page_number = int(page) if page not in (None, "") else 1
    except (TypeError, ValueError):
        raise ValidationError("page 必须是正整数。")
    try:
        size = int(page_size) if page_size not in (None, "") else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        raise ValidationError("page_size 必须是正整数。")
    if page_number < 1:
        raise ValidationError("page 从 1 开始。")
    if size < 1:
        raise ValidationError("page_size 至少为 1。")
    if size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size 最大为 {MAX_PAGE_SIZE}。")
    return page_number, size


def offset_of(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def paginate(items: list[Any], total: int, page: int, page_size: int) -> dict[str, Any]:
    return {"items": items, "total": total, "page": page, "page_size": page_size}