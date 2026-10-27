"""纯块文档校验：结构、边界、引用抽取与原子块操作。"""

from __future__ import annotations

import copy

import pytest

from litworkbench import note_blocks as blocks
from litworkbench.errors import LitError


def text_block(block_id: str, text: str = "", *, kind: str = "paragraph",
               props: dict | None = None, children: list | None = None) -> dict:
    return {
        "id": block_id,
        "type": kind,
        "props": dict(props or {}),
        "content": [{"type": "text", "text": text, "styles": {}}] if text else [],
        "children": list(children or []),
    }


def body(*items: dict) -> dict:
    return {"schema_version": 1, "blocks": list(items)}


def find(block_list: list[dict], block_id: str) -> dict | None:
    for block in block_list:
        if block["id"] == block_id:
            return block
        found = find(block.get("children") or [], block_id)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------- 基本结构


def test_body_shape_and_unicode_are_preserved():
    source = body(text_block("块_😀", "中文😀 café"))
    normalized = blocks.normalize_body(source)
    assert normalized == source
    assert blocks.extract_text(normalized) == "中文😀 café"
    assert "中文😀" in blocks.canonical_json(normalized)


def test_numbered_list_start_is_preserved():
    source = body(text_block("ordered", "从第五条开始", kind="numberedListItem", props={"start": 5}))
    assert blocks.normalize_body(source) == source


def test_unknown_block_type_and_unknown_props_are_rejected():
    with pytest.raises(LitError):
        blocks.normalize_body(body(text_block("b1", "x", kind="databaseView")))
    with pytest.raises(LitError):
        blocks.normalize_body(body(text_block("b2", "x", props={"html": "<b>x</b>"})))


def test_wrong_schema_version_and_unknown_top_level_keys_are_rejected():
    with pytest.raises(LitError):
        blocks.normalize_body({"schema_version": 2, "blocks": []})
    with pytest.raises(LitError):
        blocks.normalize_body({"schema_version": 1, "blocks": [], "html": "<p>x</p>"})


def test_duplicate_block_ids_are_rejected():
    with pytest.raises(LitError):
        blocks.normalize_body(body(text_block("same", "一"), text_block("same", "二")))


def test_empty_block_id_is_rejected():
    with pytest.raises(LitError):
        blocks.normalize_body(body(text_block("   ", "x")))


def test_block_tree_cycle_is_rejected():
    first = text_block("a", "A")
    first["children"].append(first)
    with pytest.raises(LitError):
        blocks.normalize_body(body(first))


# ---------------------------------------------------------------- 边界


def test_body_byte_limit_is_one_mib():
    huge = text_block("big", "汉" * (400 * 1024))
    assert blocks.body_size(body(huge)) > blocks.MAX_BODY_BYTES
    with pytest.raises(LitError):
        blocks.normalize_body(body(huge))


def test_block_count_limit_is_2000():
    items = [text_block(f"b{index}") for index in range(blocks.MAX_BLOCKS)]
    assert len(blocks.normalize_body(body(*items))["blocks"]) == blocks.MAX_BLOCKS
    with pytest.raises(LitError):
        blocks.normalize_body(body(*items, text_block("one_too_many")))


def test_nesting_limit_is_eight_levels():
    def nested(depth: int) -> dict:
        current = text_block(f"l{depth}", "leaf")
        for level in range(depth - 1, 0, -1):
            current = text_block(f"l{level}", "node", children=[current])
        return current

    assert blocks.normalize_body(body(nested(blocks.MAX_DEPTH)))["blocks"]
    with pytest.raises(LitError):
        blocks.normalize_body(body(nested(blocks.MAX_DEPTH + 1)))


# ---------------------------------------------------------------- 引用


def test_extract_references_reads_stable_target_ids():
    page = text_block("p", kind="pageRef", props={"pageId": "pg_target"})
    doc = text_block("d", kind="documentRef", props={"documentId": "doc_target"})
    evidence = text_block("e", kind="evidenceRef", props={"evidenceId": "ev_target"})
    refs = blocks.extract_references(body(page, doc, evidence))
    assert [(item["kind"], item["target_id"], item["block_id"]) for item in refs] == [
        ("page", "pg_target", "p"),
        ("document", "doc_target", "d"),
        ("evidence", "ev_target", "e"),
    ]


def test_reference_block_without_target_is_rejected():
    with pytest.raises(LitError):
        blocks.normalize_body(body(text_block("p", kind="pageRef", props={})))
    with pytest.raises(LitError):
        blocks.normalize_body(body(text_block("i", kind="image", props={})))


# ---------------------------------------------------------------- 行内链接

def inline_link(href: str, label: str = "白塔") -> dict:
    return {
        "type": "link",
        "href": href,
        "content": [{"type": "text", "text": label, "styles": {}}],
    }


def test_blocknote_inline_link_is_preserved_and_extracted_as_text():
    source = body(text_block("p", "见"))
    source["blocks"][0]["content"].append(inline_link("https://example.com/a?b=1"))
    source["blocks"][0]["content"].append({
        "type": "text", "text": "。", "styles": {"bold": True},
    })
    normalized = blocks.normalize_body(source)
    link = normalized["blocks"][0]["content"][1]
    assert link == inline_link("https://example.com/a?b=1")
    assert blocks.extract_text(normalized) == "见白塔。"


@pytest.mark.parametrize("href", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "https:///missing-host",
    "https://example.com/\n<script>",
])
def test_inline_link_rejects_unsafe_or_malformed_schemes(href):
    source = body(text_block("p", "见"))
    source["blocks"][0]["content"].append(inline_link(href))
    with pytest.raises(LitError):
        blocks.normalize_body(source)


def test_inline_link_rejects_nested_links_and_unknown_fields():
    source = body(text_block("p", "见"))
    nested = inline_link("https://example.com")
    nested["content"].append(inline_link("https://example.org"))
    source["blocks"][0]["content"].append(nested)
    with pytest.raises(LitError):
        blocks.normalize_body(source)

    source = body(text_block("p", "见"))
    bad = inline_link("https://example.com")
    bad["onclick"] = "alert(1)"
    source["blocks"][0]["content"].append(bad)
    with pytest.raises(LitError):
        blocks.normalize_body(source)


# ---------------------------------------------------------------- 块操作


def test_insert_replace_move_operations():
    initial = body(
        text_block("a", "A", children=[text_block("a1", "A1")]),
        text_block("b", "B"),
    )
    result = blocks.apply_operations(initial, [
        {"op": "insert", "parent_block_id": "a", "index": 1,
         "block": text_block("c", "C")},
        {"op": "move", "block_id": "b", "parent_block_id": "a", "index": 0},
        {"op": "replace", "block_id": "a1", "block": text_block("a1", "A1 新")},
    ])
    a = find(result["blocks"], "a")
    assert [child["id"] for child in a["children"]] == ["b", "a1", "c"]
    assert find(result["blocks"], "a1")["content"][0]["text"] == "A1 新"
    assert initial == body(
        text_block("a", "A", children=[text_block("a1", "A1")]),
        text_block("b", "B"),
    )


def test_operations_are_atomic_when_a_later_operation_fails():
    initial = body(text_block("a", "A"))
    with pytest.raises(LitError):
        blocks.apply_operations(initial, [
            {"op": "insert", "parent_block_id": None, "index": 1,
             "block": text_block("b", "B")},
            {"op": "replace", "block_id": "missing", "block": text_block("missing", "X")},
        ])
    assert initial == body(text_block("a", "A"))


def test_operation_count_is_limited_to_100():
    operations = [
        {"op": "insert", "parent_block_id": None, "index": index,
         "block": text_block(f"n{index}", "x")}
        for index in range(blocks.MAX_EDIT_OPERATIONS + 1)
    ]
    with pytest.raises(LitError):
        blocks.apply_operations(body(), operations)


def test_move_cannot_target_itself_or_a_descendant():
    initial = body(text_block("a", "A", children=[text_block("a1", "A1")]))
    with pytest.raises(LitError):
        blocks.apply_operations(initial, [
            {"op": "move", "block_id": "a", "parent_block_id": "a1", "index": 0},
        ])
    with pytest.raises(LitError):
        blocks.apply_operations(initial, [
            {"op": "move", "block_id": "a", "parent_block_id": "a", "index": 0},
        ])


def test_replace_cannot_change_stable_block_id():
    initial = body(text_block("a", "A"))
    with pytest.raises(LitError):
        blocks.apply_operations(initial, [
            {"op": "replace", "block_id": "a", "block": text_block("other", "B")},
        ])


def test_unknown_operation_is_rejected():
    with pytest.raises(LitError):
        blocks.apply_operations(body(), [{"op": "delete", "block_id": "a"}])
    with pytest.raises(LitError):
        blocks.apply_operations(body(), [])
