from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, require_user

router = APIRouter(prefix="/api/memory", tags=["memory"])
User = Annotated[Ctx, Depends(require_user)]


class MemBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    pinned: bool = False


class MemPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    pinned: bool | None = None


@router.get("")
def list_memory(ctx: User):
    return {"memories": ctx.p.memory.list(ctx.uid)}


@router.post("")
async def add_memory(body: MemBody, ctx: User):
    return {"memory": await ctx.p.memory.add(ctx.uid, body.content, source="user", pinned=body.pinned)}


@router.patch("/{mem_id}")
async def update_memory(mem_id: str, body: MemPatch, ctx: User):
    row = await ctx.p.memory.update(ctx.uid, mem_id, body.content, body.pinned)
    if not row:
        raise ApiError(404, "not_found", "記憶が見つかりません")
    return {"memory": row}


@router.delete("/{mem_id}")
def delete_memory(mem_id: str, ctx: User):
    if not ctx.p.memory.delete(ctx.uid, mem_id):
        raise ApiError(404, "not_found", "記憶が見つかりません")
    return {"ok": True}


@router.get("/search")
async def search_memory(q: str, ctx: User):
    return {"results": await ctx.p.memory.search(ctx.uid, q[:500], k=10, min_score=0.0)}
