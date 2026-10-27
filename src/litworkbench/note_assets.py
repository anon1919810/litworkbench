"""笔记图片资产：真实格式校验、按内容哈希不可变保存与受控读取。

资产独立于文献原件：只接受 PNG/JPEG/WebP/GIF，单文件不超过 20 MiB。
数据库保存的是相对工作区路径；接口只返回稳定资产 ID 与认证下载地址，
从不暴露本机绝对路径。相同内容重复上传复用同一个不可变对象。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from litworkbench.errors import NotFoundError, UnsupportedFileError, ValidationError
from litworkbench.store import Workspace, ensure_write_transaction, utcnow

MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp", "image/gif": ".gif"}
_PIL_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg",
                "WEBP": "image/webp", "GIF": "image/gif"}


class NoteAssetError(ValidationError):
    """资产内容或归属不合法。"""

    code = "NOTE_ASSET_INVALID"
    status_code = 422


class NoteAssetTooLargeError(NoteAssetError):
    code = "FILE_TOO_LARGE"
    status_code = 413


class UnsupportedNoteAssetError(UnsupportedFileError):
    code = "UNSUPPORTED_MEDIA_TYPE"
    status_code = 415


# ------------------------------------------------------------------ 路径与格式


def assets_dir(workspace: Workspace) -> Path:
    return workspace.root / "note-assets"


def _safe_filename(raw: str) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"<>|:*?').strip()
    if not name:
        name = "图片"
    return name[:200]


def detect_image(data: bytes) -> tuple[str, str]:
    """按真实内容判断图片类型；不用文件名或上传头决定类型。"""
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        declared = "image/png"
    elif len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        declared = "image/jpeg"
    elif len(data) >= 6 and data[:6] in {b"GIF87a", b"GIF89a"}:
        declared = "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        declared = "image/webp"
    else:
        raise UnsupportedNoteAssetError(
            "只接受 PNG/JPEG/WebP/GIF 图片；文件内容不是这几种格式。"
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            actual = _PIL_FORMATS.get(image.format or "")
    except Exception as exc:  # noqa: BLE001 - Pillow 的异常类型很多
        raise UnsupportedNoteAssetError(
            f"图片内容无法解码，已拒绝上传：{type(exc).__name__}"
        ) from exc
    if actual != declared:
        raise UnsupportedNoteAssetError("图片内容与声明的格式不一致，已拒绝上传。")
    return declared, _EXTENSIONS[declared]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ 保存与读取


def store_asset(workspace: Workspace, *, filename: str, data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not data:
        raise NoteAssetError("图片是空文件。")
    if len(data) > MAX_IMAGE_BYTES:
        raise NoteAssetTooLargeError("单张图片不能超过 20 MiB。")
    media_type, suffix = detect_image(data)
    digest = _sha256_bytes(data)
    directory = assets_dir(workspace) / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}{suffix}"
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        existing = conn.execute(
            "SELECT * FROM note_assets WHERE sha256 = ?", (digest,)
        ).fetchone()
        if existing is not None:
            return asset_view(dict(existing))
        published = False
        if target.exists():
            # 同名哈希文件必须仍是同一内容；否则不接管未知文件。
            if _sha256_bytes(target.read_bytes()) != digest:
                raise NoteAssetError("内容哈希命名冲突，已中止保存。")
        else:
            temporary = directory / f".{digest}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                temporary.replace(target)
                published = True
            finally:
                temporary.unlink(missing_ok=True)
        asset_id = f"asset_{uuid.uuid4().hex[:12]}"
        now = utcnow()
        try:
            conn.execute(
                "INSERT INTO note_assets(id, sha256, stored_path, media_type, byte_size, "
                "filename, workspace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, digest, target.relative_to(workspace.root).as_posix(),
                 media_type, len(data), _safe_filename(filename), workspace.root.name, now),
            )
        except BaseException:
            if published and conn.in_transaction:
                target.unlink(missing_ok=True)
            raise
        row = conn.execute("SELECT * FROM note_assets WHERE id = ?", (asset_id,)).fetchone()
        return asset_view(dict(row))


def get_asset(workspace: Workspace, asset_id: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        row = conn.execute("SELECT * FROM note_assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到笔记图片：{asset_id}")
    return dict(row)


def asset_path(workspace: Workspace, record: dict[str, Any]) -> Path:
    directory = assets_dir(workspace).resolve()
    candidate = (workspace.root / str(record["stored_path"])).resolve()
    if not candidate.is_relative_to(directory):
        raise NotFoundError("笔记图片不在受控目录内，拒绝读取。")
    if not candidate.exists():
        raise NotFoundError(f"笔记图片文件缺失：{record['filename']}")
    return candidate


def asset_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "sha256": record["sha256"],
        "media_type": record["media_type"],
        "byte_size": int(record["byte_size"]),
        "filename": record["filename"],
        "url": f"/api/v1/note-assets/{record['id']}",
        "created_at": record["created_at"],
    }


__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "MAX_IMAGE_BYTES",
    "NoteAssetError",
    "NoteAssetTooLargeError",
    "UnsupportedNoteAssetError",
    "asset_path",
    "asset_view",
    "assets_dir",
    "detect_image",
    "get_asset",
    "store_asset",
]
