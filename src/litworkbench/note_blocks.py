"""受限块文档：结构校验、文本/引用抽取与原子块操作。

持久化格式固定为::

    {"schema_version": 1, "blocks": [块, ...]}

块以 BlockNote 基础块为兼容目标，但只接受本模块明确列出的字段与类型；
未知类型、未知属性、重复块 ID、环、超过 2000 块 / 8 层 / 1 MiB 的正文
一律拒绝，绝不悄悄丢弃客户端内容。

引用块只保存稳定目标 ID 与服务器权威快照字段；抓取与覆盖由
:mod:`litworkbench.notes` 在同一次写事务里完成。
"""

from __future__ import annotations

import copy
import json
from typing import Any, Iterable
from urllib.parse import urlsplit

from litworkbench.errors import ValidationError

SCHEMA_VERSION = 1
MAX_BODY_BYTES = 1024 * 1024
MAX_BLOCKS = 2000
MAX_DEPTH = 8
MAX_EDIT_OPERATIONS = 100
MAX_BLOCK_ID_LENGTH = 200

BLOCK_TYPES = frozenset({
    "paragraph", "heading", "bulletListItem", "numberedListItem", "checkListItem",
    "quote", "divider", "link", "image", "pageRef", "documentRef", "evidenceRef",
})
TEXT_BLOCK_TYPES = frozenset({
    "paragraph", "heading", "bulletListItem", "numberedListItem", "checkListItem",
    "quote", "link",
})
EMPTY_CONTENT_TYPES = frozenset({
    "divider", "image", "pageRef", "documentRef", "evidenceRef",
})
REFERENCE_TYPES = {"pageRef": "page", "documentRef": "document", "evidenceRef": "evidence"}

_TOP_KEYS = frozenset({"schema_version", "blocks"})
_BLOCK_KEYS = frozenset({"id", "type", "props", "content", "children"})
_TEXT_INLINE_KEYS = frozenset({"type", "text", "styles"})
_LINK_INLINE_KEYS = frozenset({"type", "href", "content"})
_STYLE_KEYS = frozenset({
    "bold", "italic", "underline", "strike", "code", "textColor", "backgroundColor",
})
_COMMON_PROPS = frozenset({"textColor", "backgroundColor", "textAlignment"})
_ALLOWED_PROPS: dict[str, frozenset[str]] = {
    "paragraph": _COMMON_PROPS,
    "heading": _COMMON_PROPS | {"level"},
    "bulletListItem": _COMMON_PROPS,
    "numberedListItem": _COMMON_PROPS | {"start"},
    "checkListItem": _COMMON_PROPS | {"checked"},
    "quote": _COMMON_PROPS,
    "divider": frozenset(),
    "link": frozenset({"url"}),
    "image": frozenset({
        "assetId", "caption", "width", "sha256", "media_type", "byte_size", "src",
        "missing",
    }),
    "pageRef": frozenset({
        "pageId", "title", "icon", "archived", "effective_archived", "missing",
    }),
    "documentRef": frozenset({
        "documentId", "title", "filename", "author", "year", "missing",
    }),
    "evidenceRef": frozenset({
        "evidenceId", "quote", "review_status", "doc_id", "unit_id", "version",
        "locator", "filename", "title", "file_page", "printed_page",
        "printed_page_status", "missing",
    }),
}

_REQUIRED_TARGET = {"image": "assetId", "pageRef": "pageId",
                    "documentRef": "documentId", "evidenceRef": "evidenceId"}


class NoteBlockError(ValidationError):
    """块文档不符合受限契约。"""

    code = "NOTE_BLOCK_INVALID"
    status_code = 422


def empty_body() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "blocks": []}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def body_size(body: Any) -> int:
    return len(canonical_json(body).encode("utf-8"))


def normalize_body(body: Any) -> dict[str, Any]:
    """校验并返回独立副本；不读数据库、不修改输入。"""
    if not isinstance(body, dict):
        raise NoteBlockError("页面正文必须是对象。")
    unknown = set(body) - _TOP_KEYS
    if unknown:
        raise NoteBlockError(f"正文包含未知字段：{'、'.join(sorted(unknown))}。")
    if body.get("schema_version") != SCHEMA_VERSION:
        raise NoteBlockError(f"正文 schema_version 必须是 {SCHEMA_VERSION}。")
    raw_blocks = body.get("blocks")
    if not isinstance(raw_blocks, list):
        raise NoteBlockError("正文 blocks 必须是数组。")
    seen_ids: set[str] = set()
    seen_objects: set[int] = set()
    counter = [0]
    blocks = [
        _normalize_block(item, depth=1, seen_ids=seen_ids, seen_objects=seen_objects,
                         counter=counter)
        for item in raw_blocks
    ]
    result = {"schema_version": SCHEMA_VERSION, "blocks": blocks}
    if body_size(result) > MAX_BODY_BYTES:
        raise NoteBlockError("页面正文超过 1 MiB 上限，请删除部分内容后保存。")
    return result


def _normalize_block(raw: Any, *, depth: int, seen_ids: set[str],
                     seen_objects: set[int], counter: list[int]) -> dict[str, Any]:
    if depth > MAX_DEPTH:
        raise NoteBlockError(f"块嵌套超过 {MAX_DEPTH} 层上限。")
    if not isinstance(raw, dict):
        raise NoteBlockError("每个块必须是对象。")
    identity = id(raw)
    if identity in seen_objects:
        raise NoteBlockError("块树中存在环或重复引用。")
    seen_objects.add(identity)
    unknown = set(raw) - _BLOCK_KEYS
    if unknown:
        raise NoteBlockError(f"块包含未知字段：{'、'.join(sorted(unknown))}。")
    block_id = raw.get("id")
    if not isinstance(block_id, str) or not block_id.strip():
        raise NoteBlockError("块 ID 必须是非空字符串。")
    if len(block_id) > MAX_BLOCK_ID_LENGTH:
        raise NoteBlockError(f"块 ID 不能超过 {MAX_BLOCK_ID_LENGTH} 个字符。")
    if block_id in seen_ids:
        raise NoteBlockError(f"块 ID 重复：{block_id}")
    seen_ids.add(block_id)
    counter[0] += 1
    if counter[0] > MAX_BLOCKS:
        raise NoteBlockError(f"页面正文最多 {MAX_BLOCKS} 个块。")

    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in BLOCK_TYPES:
        raise NoteBlockError(f"不支持的块类型：{kind!r}")
    props = raw.get("props", {})
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise NoteBlockError(f"块 {block_id} 的 props 必须是对象。")
    unknown_props = set(props) - _ALLOWED_PROPS[kind]
    if unknown_props:
        raise NoteBlockError(
            f"块 {block_id} 包含不支持的属性：{'、'.join(sorted(unknown_props))}。"
        )
    _validate_props(block_id, kind, props)

    content = raw.get("content", [])
    if content is None:
        content = []
    if not isinstance(content, list):
        raise NoteBlockError(f"块 {block_id} 的 content 必须是数组。")
    if kind in EMPTY_CONTENT_TYPES and content:
        raise NoteBlockError(f"{kind} 块不能包含行内文字。")
    inline = [_normalize_inline(block_id, item) for item in content]

    children = raw.get("children", [])
    if children is None:
        children = []
    if not isinstance(children, list):
        raise NoteBlockError(f"块 {block_id} 的 children 必须是数组。")
    normalized_children = [
        _normalize_block(item, depth=depth + 1, seen_ids=seen_ids,
                         seen_objects=seen_objects, counter=counter)
        for item in children
    ]
    return {
        "id": block_id,
        "type": kind,
        "props": copy.deepcopy(props),
        "content": inline,
        "children": normalized_children,
    }


def _normalize_inline(block_id: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise NoteBlockError(f"块 {block_id} 的行内内容必须是对象。")
    kind = raw.get("type")
    if kind == "text":
        unknown = set(raw) - _TEXT_INLINE_KEYS
        if unknown:
            raise NoteBlockError(
                f"块 {block_id} 的行内文字包含未知字段：{'、'.join(sorted(unknown))}。"
            )
        return _normalize_styled_text(block_id, raw)
    if kind == "link":
        unknown = set(raw) - _LINK_INLINE_KEYS
        if unknown:
            raise NoteBlockError(
                f"块 {block_id} 的行内链接包含未知字段：{'、'.join(sorted(unknown))}。"
            )
        href = _require_http_url(raw.get("href"), f"块 {block_id} 的行内链接")
        content = raw.get("content")
        if not isinstance(content, list) or not content:
            raise NoteBlockError(f"块 {block_id} 的行内链接必须包含非空 content 数组。")
        normalized_content: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                raise NoteBlockError(
                    f"块 {block_id} 的行内链接只接受 text 内容，不支持嵌套链接。"
                )
            unknown_item = set(item) - _TEXT_INLINE_KEYS
            if unknown_item:
                raise NoteBlockError(
                    f"块 {block_id} 的行内链接文字包含未知字段："
                    f"{'、'.join(sorted(unknown_item))}。"
                )
            normalized_content.append(_normalize_styled_text(block_id, item))
        return {"type": "link", "href": href, "content": normalized_content}
    raise NoteBlockError(f"块 {block_id} 只支持 text 或受限 link 行内内容，收到 {kind!r}。")


def _normalize_styled_text(block_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    text = raw.get("text")
    if not isinstance(text, str):
        raise NoteBlockError(f"块 {block_id} 的行内文字必须是字符串。")
    styles = raw.get("styles", {})
    if styles is None:
        styles = {}
    if not isinstance(styles, dict):
        raise NoteBlockError(f"块 {block_id} 的行内样式必须是对象。")
    unknown_styles = set(styles) - _STYLE_KEYS
    if unknown_styles:
        raise NoteBlockError(
            f"块 {block_id} 包含未知行内样式：{'、'.join(sorted(unknown_styles))}。"
        )
    for key, value in styles.items():
        if key in {"bold", "italic", "underline", "strike", "code"}:
            if not isinstance(value, bool):
                raise NoteBlockError(f"块 {block_id} 的行内样式 {key} 必须是布尔值。")
        elif not isinstance(value, str):
            raise NoteBlockError(f"块 {block_id} 的行内样式 {key} 必须是字符串。")
    return {"type": "text", "text": text, "styles": copy.deepcopy(styles)}


def _require_http_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise NoteBlockError(f"{label} 必须是字符串。")
    if not value or len(value) > 2000 or value != value.strip():
        raise NoteBlockError(f"{label} 必须是不超过 2000 字符的 http/https 地址。")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise NoteBlockError(f"{label} 不能包含空白或控制字符。")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise NoteBlockError(f"{label} 不是合法 URL。") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise NoteBlockError(f"{label} 只允许 http/https 且必须包含主机名。")
    return value


def _validate_props(block_id: str, kind: str, props: dict[str, Any]) -> None:
    if kind == "numberedListItem" and "start" in props:
        if type(props["start"]) is not int or props["start"] < 1:
            raise NoteBlockError("编号列表的起始值必须是正整数。")
    target = _REQUIRED_TARGET.get(kind)
    if target is not None:
        value = props.get(target)
        if not isinstance(value, str) or not value.strip():
            raise NoteBlockError(f"{kind} 块 {block_id} 缺少 {target}。")
    if kind == "heading":
        level = props.get("level", 1)
        if type(level) is not int or not 1 <= level <= 6:
            raise NoteBlockError(f"heading 块 {block_id} 的 level 必须是 1–6。")
    if kind == "checkListItem" and "checked" in props:
        if not isinstance(props["checked"], bool):
            raise NoteBlockError(f"checkListItem 块 {block_id} 的 checked 必须是布尔值。")
    if kind == "link":
        _require_http_url(props.get("url"), f"link 块 {block_id} 的 url")
    for key in ("textColor", "backgroundColor", "textAlignment"):
        if key in props and not isinstance(props[key], str):
            raise NoteBlockError(f"块 {block_id} 的 {key} 必须是字符串。")
    for key in ("title", "filename", "author", "year", "quote", "review_status",
                "doc_id", "unit_id", "version", "locator", "printed_page",
                "printed_page_status", "media_type", "sha256", "src", "caption",
                "icon"):
        if key in props and not isinstance(props[key], str):
            raise NoteBlockError(f"块 {block_id} 的 {key} 必须是字符串。")
    for key in ("archived", "effective_archived", "missing"):
        if key in props and not isinstance(props[key], bool):
            raise NoteBlockError(f"块 {block_id} 的 {key} 必须是布尔值。")
    for key in ("file_page",):
        if key in props and props[key] is not None and type(props[key]) is not int:
            raise NoteBlockError(f"块 {block_id} 的 {key} 必须是整数或 null。")
    for key in ("byte_size", "width"):
        if key in props and props[key] is not None and type(props[key]) is not int:
            raise NoteBlockError(f"块 {block_id} 的 {key} 必须是整数或 null。")


def walk_blocks(body: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """深度优先遍历块（调用方应先 :func:`normalize_body`）。"""
    stack = list(reversed(body.get("blocks") or []))
    while stack:
        block = stack.pop()
        yield block
        stack.extend(reversed(block.get("children") or []))


def _inline_text(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") == "link":
            parts.append(_inline_text(item.get("content") or []))
        else:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def extract_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in walk_blocks(body):
        text = _inline_text(block.get("content") or [])
        if text:
            parts.append(text)
    return "\n".join(parts)


def extract_references(body: dict[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for block in walk_blocks(body):
        kind = REFERENCE_TYPES.get(block.get("type", ""))
        if kind is None:
            continue
        props = block.get("props") or {}
        target_key = _REQUIRED_TARGET[block["type"]]
        references.append({
            "block_id": block["id"],
            "block_type": block["type"],
            "kind": kind,
            "target_id": props[target_key],
            "props": copy.deepcopy(props),
        })
    return references


# ------------------------------------------------------------------ 块操作


def apply_operations(body: dict[str, Any], operations: Any) -> dict[str, Any]:
    """按顺序原子应用 insert / replace / move；不修改输入。"""
    if not isinstance(operations, list) or not operations:
        raise NoteBlockError("块操作必须是非空数组。")
    if len(operations) > MAX_EDIT_OPERATIONS:
        raise NoteBlockError(f"一次最多 {MAX_EDIT_OPERATIONS} 个块操作。")
    working = normalize_body(body)
    for operation in operations:
        _apply_one(working, operation)
    return normalize_body(working)


def _apply_one(body: dict[str, Any], operation: Any) -> None:
    if not isinstance(operation, dict):
        raise NoteBlockError("块操作必须是对象。")
    kind = operation.get("op")
    if kind == "insert":
        _check_keys(operation, {"op", "parent_block_id", "index", "block"})
        parent_id = _parent_id(operation)
        index = _operation_index(operation)
        raw_block = operation.get("block")
        if not isinstance(raw_block, dict):
            raise NoteBlockError("insert 操作缺少 block。")
        block = _normalize_block(raw_block, depth=1, seen_ids=set(),
                                 seen_objects=set(), counter=[0])
        _ensure_ids_free(body, block)
        target = _children_for(body, parent_id)
        if index > len(target):
            raise NoteBlockError("insert 的 index 超出目标子块范围。")
        target.insert(index, block)
        return
    if kind == "replace":
        _check_keys(operation, {"op", "block_id", "block"})
        block_id = _required_block_id(operation)
        raw_block = operation.get("block")
        if not isinstance(raw_block, dict):
            raise NoteBlockError("replace 操作缺少 block。")
        replacement = _normalize_block(raw_block, depth=1, seen_ids=set(),
                                       seen_objects=set(), counter=[0])
        if replacement["id"] != block_id:
            raise NoteBlockError("replace 不能改变稳定块 ID。")
        block, parent, index = _find_with_parent(body, block_id)
        old_ids = _ids_of(block)
        new_ids = _ids_of(replacement)
        existing = {item for item in _all_ids(body) if item not in old_ids}
        if existing & new_ids:
            raise NoteBlockError("replace 后的块 ID 与页面内其他块重复。")
        parent[index] = replacement
        return
    if kind == "move":
        _check_keys(operation, {"op", "block_id", "parent_block_id", "index"})
        block_id = _required_block_id(operation)
        parent_id = _parent_id(operation)
        index = _operation_index(operation)
        block, old_parent, old_index = _find_with_parent(body, block_id)
        if parent_id in _ids_of(block):
            raise NoteBlockError("块不能移动到自身或自己的后代之下。")
        # index 按移除后的目标列表解释，与常见编辑器拖放语义一致。
        del old_parent[old_index]
        target = _children_for(body, parent_id)
        if index > len(target):
            raise NoteBlockError("move 的 index 超出目标子块范围。")
        target.insert(index, block)
        return
    raise NoteBlockError(f"未知块操作：{kind!r}")


def _check_keys(operation: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(operation) - allowed
    if unknown:
        raise NoteBlockError(f"块操作包含未知字段：{'、'.join(sorted(unknown))}。")
    for key in allowed - {"op"}:
        if key not in operation:
            raise NoteBlockError(f"块操作缺少字段：{key}。")


def _parent_id(operation: dict[str, Any]) -> str | None:
    value = operation.get("parent_block_id")
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise NoteBlockError("parent_block_id 必须是非空字符串或 null。")
    return value


def _operation_index(operation: dict[str, Any]) -> int:
    value = operation.get("index")
    if type(value) is not int or value < 0:
        raise NoteBlockError("index 必须是非负整数。")
    return value


def _required_block_id(operation: dict[str, Any]) -> str:
    value = operation.get("block_id")
    if not isinstance(value, str) or not value.strip():
        raise NoteBlockError("block_id 必须是非空字符串。")
    return value


def _children_for(body: dict[str, Any], parent_id: str | None) -> list[dict[str, Any]]:
    if parent_id is None:
        return body["blocks"]
    block, _parent, _index = _find_with_parent(body, parent_id)
    return block["children"]


def _find_with_parent(body: dict[str, Any], block_id: str
                      ) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    def search(items: list[dict[str, Any]]):
        for index, block in enumerate(items):
            if block["id"] == block_id:
                return block, items, index
            found = search(block["children"])
            if found is not None:
                return found
        return None

    found = search(body["blocks"])
    if found is None:
        raise NoteBlockError(f"块 ID 不存在：{block_id}")
    return found


def _ids_of(block: dict[str, Any]) -> set[str]:
    return {item["id"] for item in _walk_from(block)}


def _walk_from(block: dict[str, Any]) -> Iterable[dict[str, Any]]:
    stack = [block]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.get("children") or [])


def _all_ids(body: dict[str, Any]) -> set[str]:
    return {block["id"] for block in walk_blocks(body)}


def _ensure_ids_free(body: dict[str, Any], block: dict[str, Any]) -> None:
    duplicates = _all_ids(body) & _ids_of(block)
    if duplicates:
        raise NoteBlockError(f"插入的块 ID 已存在：{'、'.join(sorted(duplicates))}")
