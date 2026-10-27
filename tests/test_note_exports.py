"""页面导出包：JSON / Markdown / ZIP 的内容、资产与引用清单。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from litworkbench import note_assets
from litworkbench.errors import LitError
from litworkbench.evidence import save_evidence
from litworkbench.ingest import import_document
from litworkbench.note_exports import (
    create_page_export,
    get_page_export,
    page_export_path,
)
from litworkbench.notes import create_page

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdacd\xf8"
    b"\x0f\x00\x01\x05\x01\x01\x27\x18\xe3f\x00\x00\x00\x00IEND\xaeB`\x82"
)


def text_block(block_id: str, text: str = "", *, kind: str = "paragraph",
               props: dict | None = None) -> dict:
    return {
        "id": block_id,
        "type": kind,
        "props": dict(props or {}),
        "content": [{"type": "text", "text": text, "styles": {}}] if text else [],
        "children": [],
    }


def package_of(path):
    with zipfile.ZipFile(path) as package:
        return {name: package.read(name) for name in package.namelist()}


def seeded(workspace, fixtures):
    document = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(document["id"])[0]
    evidence = save_evidence(workspace, unit["id"], "白塔位于县城北侧")
    asset = note_assets.store_asset(workspace, filename="白塔.png", data=PNG_1X1)
    page = create_page(
        workspace,
        title="导出 / 白塔",
        parent_id=None,
        body={
            "schema_version": 1,
            "blocks": [
                text_block("title", "白塔调查", kind="heading", props={"level": 2}),
                text_block("body", "<script>alert(1)</script> 白塔位于城北。"),
                {
                    "id": "inline-link", "type": "paragraph", "props": {},
                    "content": [
                        {"type": "text", "text": "详见", "styles": {}},
                        {"type": "link", "href": "https://example.com/a?b=1",
                         "content": [{"type": "text", "text": "白塔资料",
                                      "styles": {}}]},
                    ],
                    "children": [],
                },
                text_block("img", kind="image", props={"assetId": asset["id"]}),
                text_block("ev", kind="evidenceRef", props={"evidenceId": evidence["id"]}),
                text_block("doc", kind="documentRef",
                           props={"documentId": document["id"]}),
            ],
        },
        properties={"tags": ["文物"], "status": "done",
                    "document_ids": [document["id"]]},
        request_id="export-create",
    )
    return {"page": page, "asset": asset, "evidence": evidence, "document": document}


def test_json_export_is_a_package_not_an_import(workspace, fixtures):
    data = seeded(workspace, fixtures)
    record = create_page_export(workspace, data["page"]["id"], "json")
    assert record["format"] == "json"
    assert record["filename"].endswith(".zip")
    assert record["reference_count"] >= 2 and record["asset_count"] == 1
    files = package_of(page_export_path(workspace, record))
    assert {"page.json", "references.json", "manifest.json"} <= set(files)
    payload = json.loads(files["page.json"])
    assert payload["kind"] == "note_page_export"
    assert payload["page"]["id"] == data["page"]["id"]
    inline = next(block for block in payload["page"]["body"]["blocks"]
                  if block["id"] == "inline-link")
    assert inline["content"][1]["type"] == "link"
    assert inline["content"][1]["href"] == "https://example.com/a?b=1"
    assert inline["content"][1]["content"][0]["text"] == "白塔资料"
    assert payload["links"] == {"page_import": "not_supported"}
    references = json.loads(files["references.json"])
    targets = {item["target_id"] for item in references["items"]}
    assert {data["evidence"]["id"], data["document"]["id"]} <= targets
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "json"
    assert manifest["asset_count"] == 1
    assert manifest["limitations"]
    stored = get_page_export(workspace, record["id"])
    assert stored["page_id"] == data["page"]["id"]
    assert stored["revision"] == data["page"]["revision"]


def test_markdown_export_escapes_html_and_links_assets_relative(workspace, fixtures):
    data = seeded(workspace, fixtures)
    record = create_page_export(workspace, data["page"]["id"], "markdown")
    files = package_of(page_export_path(workspace, record))
    markdown = files["page.md"].decode("utf-8")
    assert "# 导出 / 白塔" in markdown
    assert "## 白塔调查" in markdown
    assert "白塔位于县城北侧" in markdown
    assert "[白塔资料](https://example.com/a?b=1)" in markdown
    assert "<script>" not in markdown
    assert "assets/" in markdown
    assert str(workspace.root) not in markdown
    assert b"\x89PNG" in next(value for name, value in files.items()
                              if name.startswith("assets/"))


def test_zip_export_contains_both_views(workspace, fixtures):
    data = seeded(workspace, fixtures)
    record = create_page_export(workspace, data["page"]["id"], "zip")
    files = package_of(page_export_path(workspace, record))
    assert {"page.json", "page.md", "references.json", "manifest.json"} <= set(files)
    assert json.loads(files["manifest.json"])["format"] == "zip"


def test_unknown_export_format_is_rejected(workspace, fixtures):
    data = seeded(workspace, fixtures)
    with pytest.raises(LitError):
        create_page_export(workspace, data["page"]["id"], "pdf")


def test_export_path_rejects_tampered_relative_path(workspace, fixtures):
    data = seeded(workspace, fixtures)
    record = create_page_export(workspace, data["page"]["id"], "json")
    record["stored_path"] = "../outside.zip"
    with pytest.raises(LitError):
        page_export_path(workspace, record)
