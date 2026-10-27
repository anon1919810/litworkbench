"""页面导出包：JSON / Markdown / ZIP，含笔记图片和引用清单。

首版只有页面**导出**，没有页面导入接口。每个文件都是 ZIP 包：

* ``json``：``page.json`` + ``references.json`` + ``manifest.json`` + ``assets/``；
* ``markdown``：``page.md`` + 同样的引用清单、manifest 与资产；
* ``zip``：同时包含 ``page.json`` 与 ``page.md``。

动态文字一律按纯文本/受控 Markdown 输出，不把正文当作可信 HTML；
链接只写稳定目标 ID 与 API/页面地址，不写工作区绝对路径。
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

from litworkbench import note_assets, note_blocks
from litworkbench.errors import NotFoundError, ValidationError
from litworkbench.export import md_escape
from litworkbench.notes import get_page
from litworkbench.store import Workspace, ensure_write_transaction, utcnow

FORMATS = ("json", "markdown", "zip")
LIMITATIONS = (
    "这是页面导出包，不是页面导入接口；首版不承诺 Markdown 无损往返。",
    "文献原件仍由资料库管理，包内只包含笔记图片资产。",
    "引用块保存的是稳定目标 ID 与保存时的出处快照；核验状态以导出时的数据库为准。",
    "包内不包含工作区绝对路径、会话、令牌或密钥。",
)


def page_exports_dir(workspace: Workspace) -> Path:
    return workspace.artifacts_dir / "page-exports"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reference_items(page: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for reference in note_blocks.extract_references(page["body"]):
        kind = reference["kind"]
        target_id = reference["target_id"]
        if kind == "page":
            url = f"/pages/{target_id}"
        elif kind == "document":
            url = f"/api/v1/documents/{target_id}"
        else:
            url = f"/api/v1/evidence/{target_id}"
        items.append({
            "kind": kind,
            "target_id": target_id,
            "block_id": reference["block_id"],
            "block_type": reference["block_type"],
            "snapshot": reference["props"],
            "url": url,
        })
    return items


def _collect_assets(workspace: Workspace, page: dict[str, Any]
                    ) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for block in note_blocks.walk_blocks(page["body"]):
        if block["type"] != "image":
            continue
        asset_id = block["props"].get("assetId")
        if not asset_id or asset_id in assets:
            continue
        record = note_assets.get_asset(workspace, asset_id)
        assets[asset_id] = record
    return assets


def _asset_zip_name(record: dict[str, Any]) -> str:
    suffix = Path(record["stored_path"]).suffix
    return f"assets/{record['sha256']}{suffix}"


def _inline_text(block: dict[str, Any]) -> str:
    def collect(items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in items:
            if item.get("type") == "link":
                parts.append(collect(item.get("content") or []))
            else:
                parts.append(item.get("text") or "")
        return "".join(parts)

    return collect(block.get("content") or [])


def _markdown_href(href: str) -> str:
    return (href.replace("(", "%28").replace(")", "%29")
            .replace("<", "%3C").replace(">", "%3E")
            .replace(" ", "%20"))


def _inline_markdown(block: dict[str, Any]) -> str:
    def render(items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in items:
            if item.get("type") == "link":
                label = render(item.get("content") or [])
                href = item.get("href") or ""
                if href.startswith(("http://", "https://")):
                    parts.append(f"[{label}]({_markdown_href(href)})")
                else:  # 防御旧数据：绝不为危险 scheme 生成可点击链接
                    parts.append(label)
            else:
                parts.append(md_escape(item.get("text") or ""))
        return "".join(parts)

    return render(block.get("content") or [])


def _render_markdown(page: dict[str, Any], references: list[dict[str, Any]],
                     assets: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = [f"# {md_escape(page['title'])}", ""]
    tags = "、".join(page["tags"]) if page["tags"] else "（无）"
    lines.extend([
        f"- 状态：{md_escape(page['status'])}",
        f"- 标签：{md_escape(tags)}",
        f"- 修订号：{page['revision']}",
        f"- 更新时间：{md_escape(page['updated_at'])}",
        f"- 收藏：{'是' if page['favorite'] else '否'}",
        "",
    ])
    if page["document_ids"]:
        lines.append("- 关联文献：" + "、".join(
            md_escape(value) for value in page["document_ids"]))
        lines.append("")
    if page["archived"] or page["effective_archived"]:
        lines.append("> 本页面已归档（或位于已归档父页面下），仅供阅读。")
        lines.append("")

    def render(block: dict[str, Any], depth: int = 0) -> None:
        kind = block["type"]
        text = _inline_text(block)
        rich = _inline_markdown(block)
        indent = "  " * depth
        if kind == "paragraph":
            if rich:
                lines.extend([f"{indent}{rich}", ""])
        elif kind == "heading":
            level = max(2, min(6, int(block["props"].get("level", 1))))
            heading = rich.replace("\n", " ")
            lines.extend([f"{'#' * level} {heading}", ""])
        elif kind == "bulletListItem":
            lines.append(f"{indent}- {rich}")
        elif kind == "numberedListItem":
            lines.append(f"{indent}{block['props'].get('start', 1)}. {rich}")
        elif kind == "checkListItem":
            mark = "x" if block["props"].get("checked") else " "
            lines.append(f"{indent}- [{mark}] {rich}")
        elif kind == "quote":
            quoted = rich.replace("\n", "\n> ")
            lines.extend([f"{indent}> {quoted}", ""])
        elif kind == "divider":
            lines.extend([f"{indent}---", ""])
        elif kind == "link":
            url = block["props"]["url"].replace(")", "%29")
            label = md_escape(text or url)
            lines.extend([f"{indent}[{label}]({url})", ""])
        elif kind == "image":
            asset_id = block["props"]["assetId"]
            record = assets.get(asset_id)
            caption = block["props"].get("caption") or record["filename"]
            if record is None:
                lines.extend([f"{indent}（图片缺失：{md_escape(asset_id)}）", ""])
            else:
                lines.extend([
                    f"{indent}![{md_escape(caption)}]({_asset_zip_name(record)})",
                    "",
                ])
        elif kind == "pageRef":
            props = block["props"]
            marker = "（已归档）" if props.get("effective_archived") else ""
            lines.extend([
                f"{indent}页面引用：[{md_escape(props.get('title', props['pageId']))}]"
                f"(/pages/{props['pageId']}){marker}",
                "",
            ])
        elif kind == "documentRef":
            props = block["props"]
            title = props.get("title") or props.get("filename") or props["documentId"]
            lines.extend([
                f"{indent}文献引用：{md_escape(title)}"
                f"（{md_escape(props.get('filename', ''))}）",
                "",
            ])
        elif kind == "evidenceRef":
            props = block["props"]
            quote = (props.get("quote") or "").replace("\n", "\n> ")
            lines.extend([
                f"{indent}> {md_escape(quote)}",
                "",
                f"{indent}- 出处：{md_escape(props.get('locator', ''))}",
                f"{indent}- 核验状态：{md_escape(props.get('review_status', ''))}",
                f"{indent}- 文献：{md_escape(props.get('title', ''))}"
                f"（{md_escape(props.get('filename', ''))}）",
                "",
            ])
        for child in block.get("children") or []:
            render(child, depth + 1)

    for block in page["body"]["blocks"]:
        render(block)
    if references:
        lines.extend(["## 引用清单", ""])
        for item in references:
            snapshot = item.get("snapshot") or {}
            label = snapshot.get("title") or snapshot.get("quote") or item["target_id"]
            lines.append(
                f"- {item['kind']}：{md_escape(label)}（{md_escape(item['target_id'])}）"
            )
        lines.append("")
    lines.extend(["> " + md_escape(item) for item in LIMITATIONS])
    lines.append("")
    return "\n".join(lines)


def create_page_export(workspace: Workspace, page_id: str, fmt: str = "json"
                       ) -> dict[str, Any]:
    if fmt not in FORMATS:
        raise ValidationError(f"不支持的页面导出格式：{fmt}（可选 {'/'.join(FORMATS)}）")
    page = get_page(workspace, page_id)
    references = _reference_items(page)
    assets = _collect_assets(workspace, page)
    now = utcnow()
    export_id = f"pexp_{uuid.uuid4().hex[:12]}"
    manifest = {
        "kind": "note_page_export_manifest",
        "export_id": export_id,
        "page_id": page["id"],
        "revision": page["revision"],
        "format": fmt,
        "exported_at": now,
        "reference_count": len(references),
        "asset_count": len(assets),
        "assets": [{
            "asset_id": record["id"],
            "sha256": record["sha256"],
            "media_type": record["media_type"],
            "byte_size": int(record["byte_size"]),
            "filename": record["filename"],
            "path": _asset_zip_name(record),
        } for record in assets.values()],
        "limitations": list(LIMITATIONS),
        "page_import": "not_supported",
    }
    page_payload = {
        "kind": "note_page_export",
        "exported_at": now,
        "page": page,
        "links": {"page_import": "not_supported"},
        "limitations": list(LIMITATIONS),
    }
    references_payload = {
        "kind": "note_page_references",
        "page_id": page["id"],
        "revision": page["revision"],
        "items": references,
        "limitations": list(LIMITATIONS),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        if fmt in {"json", "zip"}:
            package.writestr("page.json",
                             json.dumps(page_payload, ensure_ascii=False, indent=2) + "\n")
        if fmt in {"markdown", "zip"}:
            package.writestr("page.md", _render_markdown(page, references, assets))
        package.writestr("references.json",
                         json.dumps(references_payload, ensure_ascii=False, indent=2) + "\n")
        package.writestr("manifest.json",
                         json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for asset_id, record in assets.items():
            package.writestr(_asset_zip_name(record),
                             note_assets.asset_path(workspace, record).read_bytes())
    data = buffer.getvalue()
    directory = page_exports_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"page_{page['id']}_r{page['revision']}_{export_id[-8:]}.zip"
    target = directory / filename
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        temporary = directory / f".{filename}.{uuid.uuid4().hex[:8]}.tmp"
        published = False
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
            temporary.replace(target)
            published = True
            conn.execute(
                "INSERT INTO page_exports(id, page_id, revision, format, filename, "
                "stored_path, sha256, byte_size, reference_count, asset_count, "
                "manifest_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (export_id, page["id"], page["revision"], fmt, filename,
                 target.relative_to(workspace.root).as_posix(),
                 note_assets._sha256_bytes(data), len(data), len(references), len(assets),
                 _dumps(manifest), now),
            )
        except BaseException:
            if published and conn.in_transaction:
                target.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        row = conn.execute("SELECT * FROM page_exports WHERE id = ?", (export_id,)).fetchone()
        return _record(row)


def get_page_export(workspace: Workspace, export_id: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        row = conn.execute("SELECT * FROM page_exports WHERE id = ?", (export_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到页面导出：{export_id}")
    return _record(row)


def _record(row) -> dict[str, Any]:
    record = dict(row)
    try:
        record["manifest"] = json.loads(record.pop("manifest_json") or "{}")
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - 容错
        record["manifest"] = {}
    return record


def page_export_path(workspace: Workspace, record: dict[str, Any]) -> Path:
    directory = page_exports_dir(workspace).resolve()
    candidate = (workspace.root / str(record["stored_path"])).resolve()
    if not candidate.is_relative_to(directory):
        raise NotFoundError("页面导出文件不在受控目录内，拒绝下载。")
    if not candidate.exists():
        raise NotFoundError(f"页面导出文件缺失：{record['filename']}")
    return candidate


def export_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "page_id": record["page_id"],
        "revision": int(record["revision"]),
        "format": record["format"],
        "filename": record["filename"],
        "download_url": f"/api/v1/page-exports/{record['id']}/download",
        "sha256": record["sha256"],
        "byte_size": int(record["byte_size"]),
        "reference_count": int(record["reference_count"]),
        "asset_count": int(record["asset_count"]),
        "manifest": record.get("manifest", {}),
        "created_at": record["created_at"],
    }


__all__ = [
    "FORMATS",
    "LIMITATIONS",
    "create_page_export",
    "export_view",
    "get_page_export",
    "page_export_path",
    "page_exports_dir",
]
