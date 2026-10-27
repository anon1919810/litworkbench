"""页面 API 请求模型：只接受契约字段，多余字段直接给出结构化 422。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from litworkbench.web.schemas import StrictModel

PageStatus = Literal["not_started", "in_progress", "done"]
PageExportFormat = Literal["json", "markdown", "zip"]


class PageCreate(StrictModel):
    title: str
    parent_id: str | None = None
    body: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None
    request_id: str


class PagePatch(StrictModel):
    expected_revision: int = Field(ge=0, strict=True)
    request_id: str
    title: str | None = None
    icon: str | None = None
    tags: list[str] | None = None
    status: PageStatus | None = None
    favorite: bool | None = None
    document_ids: list[str] | None = None
    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0, strict=True)
    archived: bool | None = None
    body: dict[str, Any] | None = None


class PageRestore(StrictModel):
    expected_revision: int = Field(ge=0, strict=True)
    request_id: str
    revision: int = Field(ge=1, strict=True)


class PageBlocks(StrictModel):
    expected_revision: int = Field(ge=0, strict=True)
    request_id: str
    operations: list[dict[str, Any]]


class PageExportRequest(StrictModel):
    format: PageExportFormat
