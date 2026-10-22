"""摘录集合（专题）接口。

集合是给摘录加的**归类标签**，不是研究项目：它只有新建、改名、改说明，
没有删除接口。移出集合只删成员关系，摘录、引文、校订与核验状态一概保留。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from litworkbench import collections as core
from litworkbench.web.schemas import CollectionCreate, CollectionPatch

router = APIRouter()


def workspace_of(request: Request):
    return request.app.state.workspace


@router.get("/collections")
def list_collections(request: Request) -> dict[str, Any]:
    return {"items": core.list_collections(workspace_of(request))}


@router.post("/collections", status_code=201)
def create_collection(request: Request, body: CollectionCreate) -> dict[str, Any]:
    return core.create_collection(workspace_of(request), body.name, body.description)


@router.patch("/collections/{collection_id}")
def patch_collection(collection_id: str, request: Request,
                     body: CollectionPatch) -> dict[str, Any]:
    """改名或改说明。不提供删除：集合建立后长期保留。"""
    submitted = body.model_dump(exclude_unset=True)
    return core.update_collection(
        workspace_of(request),
        collection_id,
        name=submitted.get("name"),
        description=submitted.get("description"),
    )


@router.delete("/collections/{collection_id}")
def delete_collection_forbidden(collection_id: str, request: Request) -> dict[str, Any]:
    """明确拒绝删除集合；摘录集没有永久删除语义。"""
    from litworkbench.errors import ValidationError

    core.get_collection(workspace_of(request), collection_id)
    raise ValidationError("集合不能删除；请改名为「归档」，或把其中的摘录移出集合。")