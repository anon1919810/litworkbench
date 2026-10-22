"""请求体模型：只接受契约里有的字段，多余或缺失都给出中文错误。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from litworkbench.ingest import OCR_MODES
from litworkbench.search_index import MODES as SEARCH_MODES

#: 允许编辑的书目字段（PATCH /documents/{id}）
BIBLIOGRAPHY_FIELDS = ("title", "author", "year", "edition", "volume")

PRINTED_PAGE_STATUSES = ("none", "candidate", "confirmed")
REVIEW_STATUSES = ("pending", "accepted", "rejected", "stale")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class DocumentPatch(StrictModel):
    """仅书目字段可编辑；其他字段由导入与识别流程维护。"""

    title: str | None = None
    author: str | None = None
    year: str | None = None
    edition: str | None = None
    volume: str | None = None

    def changes(self) -> dict[str, str]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class ReprocessRequest(StrictModel):
    unit_ids: list[str] = Field(default_factory=list)
    mode: Literal["force"] = "force"
    keep_corrections: bool = True


class CandidateJobRequest(StrictModel):
    """生成 OCR 候选：只登记任务，请求体不能伪造结果或状态字段。"""

    unit_ids: list[str] = Field(default_factory=list)
    profile_id: str
    layout: Literal["original"] = "original"


class CandidateAdoptRequest(StrictModel):
    """采用候选：``expected_content_revision`` 必填且非负（CAS）。"""

    expected_content_revision: int = Field(ge=0)


class CandidateRejectRequest(StrictModel):
    reason: str = ""


class SearchRequest(StrictModel):
    query: str
    mode: Literal["phrase", "all_terms", "any_terms"] = "phrase"
    doc_ids: list[str] | None = None
    page: int = 1
    page_size: int = 30
    expected_index_revision: int | None = None


class CorrectionRequest(StrictModel):
    text: str
    reason: str
    expected_hash: str | None = None


class PrintedPageRequest(StrictModel):
    printed_page: str
    status: Literal["candidate", "confirmed"] = "candidate"


class EvidenceCreate(StrictModel):
    unit_id: str
    quote: str
    #: Unicode 码点偏移（不是 JS 的 UTF-16 偏移）
    start_offset: int = Field(ge=0)
    expected_hash: str | None = None
    category: str = ""
    note: str = ""


class EvidencePatch(StrictModel):
    review_status: Literal["pending", "accepted", "rejected", "stale"] | None = None
    note: str | None = None
    review_note: str | None = None


class EvidenceBatch(StrictModel):
    """批量整理：只处理显式 ID。核验状态不在这里批量修改。"""

    evidence_ids: list[str] = Field(default_factory=list)
    #: 空字符串表示清除分类
    category: str | None = None
    add_to_collection_id: str | None = None
    remove_from_collection_id: str | None = None


class CollectionCreate(StrictModel):
    name: str
    description: str = ""


class CollectionPatch(StrictModel):
    name: str | None = None
    description: str | None = None


class ExportFilters(StrictModel):
    """导出范围。未指定 ``review_status`` 时**只含 accepted**。"""

    doc_id: str | None = None
    category: str | None = None
    #: ``all`` 表示包含全部核验状态；其它取值为具体状态
    review_status: str | None = None
    collection_id: str | None = None


class ExportPreview(StrictModel):
    format: Literal["csv", "json", "markdown", "docx"]
    filters: ExportFilters = Field(default_factory=ExportFilters)
    evidence_ids: list[str] | None = None


class ExportCreate(ExportPreview):
    #: 预览返回的内容摘要；生成时必须原样带回，摘要不一致返回 409
    snapshot_token: str


def validate_ocr_mode(value: str | None) -> str:
    mode = (value or "auto").strip() or "auto"
    if mode not in OCR_MODES:
        from litworkbench.errors import ValidationError

        raise ValidationError(f"未知的 ocr_mode：{mode}（可选 {'/'.join(OCR_MODES)}）")
    return mode


def validate_search_mode(value: str | None) -> str:
    mode = (value or "phrase").strip() or "phrase"
    if mode not in SEARCH_MODES:
        from litworkbench.errors import ValidationError

        raise ValidationError(f"未知的检索模式：{mode}（可选 {'/'.join(SEARCH_MODES)}）")
    return mode