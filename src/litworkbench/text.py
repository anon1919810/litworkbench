"""中文友好的本地文字处理工具。

要点：

* 归一化去掉所有空白，使跨换行、跨空格的中文词组可以互相匹配；
* 归一化时保留「归一化位置 -> 原文位置」的映射，因此命中片段能还原成原文偏移；
* SQL LIKE 通配符 ``%`` ``_`` 与转义符需要按字面处理。
"""

from __future__ import annotations

from typing import Iterable

#: 单元状态
COMPLETED = "completed"
PENDING = "pending"
FAILED = "failed"

LIKE_ESCAPE = "\\"


class UnitStatus:
    """单元状态常量。"""

    COMPLETED = COMPLETED
    PENDING = PENDING
    FAILED = FAILED

    ALL = (COMPLETED, PENDING, FAILED)


def normalize(text: str) -> str:
    """去掉所有空白字符（含换行），用于中文检索。"""
    return "".join(ch for ch in text if not ch.isspace())


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回 ``(归一化文本, 位置映射)``。

    ``mapping[i]`` 是归一化文本第 ``i`` 个字符在原文中的下标。
    """
    out: list[str] = []
    mapping: list[int] = []
    for index, ch in enumerate(text):
        if ch.isspace():
            continue
        out.append(ch)
        mapping.append(index)
    return "".join(out), mapping


def _find_normalized(haystack: str, needle: str) -> tuple[int, int] | None:
    """在归一化文本中查找，返回原文空间的 ``(start, end)``。"""
    norm_needle = normalize(needle)
    if not norm_needle:
        return None
    norm_hay, mapping = normalize_with_map(haystack)
    pos = norm_hay.find(norm_needle)
    if pos < 0:
        return None
    start = mapping[pos]
    end = mapping[pos + len(norm_needle) - 1] + 1
    return start, end


def find_quote(text: str, quote: str) -> tuple[int, int] | None:
    """在 ``text`` 中定位 ``quote``，先精确匹配，再跨换行匹配。

    找不到时返回 ``None``。不会做模糊/近似匹配：引文必须来自原文。
    """
    if not quote.strip():
        return None
    exact = text.find(quote)
    if exact >= 0:
        return exact, exact + len(quote)
    return _find_normalized(text, quote)


def snippet(text: str, term: str, width: int = 60) -> str:
    """截取命中位置附近的上下文；未命中时返回开头片段。"""
    found = find_quote(text, term)
    if found is None:
        return text[:width].replace("\n", " ")
    start, end = found
    half = max(width // 2, 10)
    lo = max(0, start - half)
    hi = min(len(text), end + half)
    piece = text[lo:hi].replace("\n", " ")
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return prefix + piece + suffix


def like_pattern(term: str) -> str:
    """把检索词转换成安全的 SQL LIKE 模式（转义 % _ \\）。"""
    escaped = (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def split_terms(query: str) -> list[str]:
    """把查询串拆成若干检索词（空白分隔），去重且保持顺序。"""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in query.split():
        term = normalize(raw)
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def collapse_pdf_text(text: str) -> str:
    """统一 PDF 提取文本的换行，保留行结构。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def first_meaningful_chars(text: str, limit: int = 400) -> int:
    """统计前 ``limit`` 个字符里的有效字数（汉字/字母/数字）。"""
    count = 0
    for ch in text[:limit]:
        if ch.isalnum() or is_cjk(ch):
            count += 1
    return count


def excerpt_labels(keys: Iterable[str]) -> str:
    return "、".join(keys)