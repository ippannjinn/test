from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, require_user
from ..runners.chat import run_chat, title_from
from ..util import dumps, loads, new_id, now

router = APIRouter(prefix="/api/conversations", tags=["chat"])
User = Annotated[Ctx, Depends(require_user)]


class ConvBody(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=40000)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    mode: Literal["auto", "fast", "quality", "deep"] = "auto"
    replace_from: str | None = Field(default=None, max_length=64)  # edit: drop this message and everything after


class RegenBody(BaseModel):
    mode: Literal["auto", "fast", "quality", "deep"] = "auto"


def _truncate_from(ctx: Ctx, conv_id: str, message_id: str) -> None:
    """Deletes a message and everything after it (and stops jobs still writing into the conversation)."""
    row = ctx.p.db.one("SELECT created_at FROM messages WHERE id=? AND conversation_id=? AND user_id=?",
                       (message_id, conv_id, ctx.uid))
    if not row:
        raise ApiError(404, "not_found", "メッセージが見つかりません")
    for j in list(ctx.p.jobs.jobs.values()):
        if j.conversation_id == conv_id and j.user_id == ctx.uid:
            ctx.p.jobs.cancel(j.id, ctx.uid)
    ctx.p.db.execute("DELETE FROM messages WHERE conversation_id=? AND created_at>=?", (conv_id, row["created_at"]))


def _conv(ctx: Ctx, conv_id: str) -> dict:
    row = ctx.p.db.one("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, ctx.uid))
    if not row:
        raise ApiError(404, "not_found", "会話が見つかりません")
    return row


@router.get("")
def list_conversations(ctx: User, limit: int = 100, q: str | None = None):
    sql = "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? AND archived=0"
    params: list = [ctx.uid]
    if q:
        # title or message text (like the chat search of the big apps)
        sql += (" AND (title LIKE ? ESCAPE '\\' OR id IN (SELECT conversation_id FROM messages WHERE user_id=?"
                " AND content LIKE ? ESCAPE '\\'))")
        like = "%" + q[:50].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, ctx.uid, like]
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(min(max(limit, 1), 500))
    return {"conversations": ctx.p.db.query(sql, tuple(params))}


@router.post("")
def create_conversation(body: ConvBody, ctx: User):
    cid, ts = new_id(), now()
    ctx.p.db.execute("INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                     (cid, ctx.uid, (body.title or "新しい会話").strip()[:120], ts, ts))
    return {"conversation": _conv(ctx, cid)}


@router.get("/{conv_id}")
def get_conversation(conv_id: str, ctx: User):
    conv = _conv(ctx, conv_id)
    msgs = ctx.p.db.query("SELECT id, role, content, meta, job_id, created_at FROM messages WHERE conversation_id=?"
                          " ORDER BY created_at", (conv_id,))
    for m in msgs:
        m["meta"] = loads(m["meta"], {})
    active = [j.public() for j in ctx.p.jobs.jobs.values()
              if j.conversation_id == conv_id and j.user_id == ctx.uid and j.status in ("queued", "running")]
    return {"conversation": conv, "messages": msgs, "active_jobs": active}


@router.patch("/{conv_id}")
def rename_conversation(conv_id: str, body: ConvBody, ctx: User):
    _conv(ctx, conv_id)
    ctx.p.db.execute("UPDATE conversations SET title=? WHERE id=? AND user_id=?",
                     ((body.title or "無題").strip()[:120], conv_id, ctx.uid))
    return {"conversation": _conv(ctx, conv_id)}


@router.delete("/{conv_id}")
def delete_conversation(conv_id: str, ctx: User):
    _conv(ctx, conv_id)
    for j in list(ctx.p.jobs.jobs.values()):
        if j.conversation_id == conv_id:
            ctx.p.jobs.cancel(j.id, ctx.uid)
    ctx.p.db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, ctx.uid))
    ctx.p.files.delete_conv_workspace(ctx.uid, conv_id)
    return {"ok": True}


@router.post("/{conv_id}/messages")
async def post_message(conv_id: str, body: MessageBody, ctx: User):
    p = ctx.p
    for fid in body.attachments:
        if not p.files.get(ctx.uid, fid):
            raise ApiError(404, "file_not_found", "添付ファイルが見つかりません")
    ts = now()
    if conv_id == "new":
        conv_id = new_id()
        p.db.execute("INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                     (conv_id, ctx.uid, title_from(body.content), ts, ts))
    else:
        _conv(ctx, conv_id)
        if body.replace_from:
            _truncate_from(ctx, conv_id, body.replace_from)
    mid = new_id()
    meta = {"attachments": [{"id": f["id"], "name": f["name"], "mime": f["mime"]}
                            for f in (p.files.get(ctx.uid, x) for x in body.attachments) if f], "mode": body.mode}
    job = p.jobs.create(ctx.user, "chat", {"content": body.content, "attachments": body.attachments, "mode": body.mode,
                                           "user_message_id": mid}, conversation_id=conv_id)
    p.db.execute("INSERT INTO messages(id, conversation_id, user_id, role, content, meta, job_id, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?)", (mid, conv_id, ctx.uid, "user", body.content, dumps(meta), job.id, ts))
    p.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, conv_id))
    p.jobs.start(job, lambda j: run_chat(p, j))
    return {"conversation_id": conv_id, "user_message_id": mid, "job": job.public()}


@router.post("/{conv_id}/regenerate")
async def regenerate(conv_id: str, body: RegenBody, ctx: User):
    """Re-answers the last user message: removes the replies after it and runs it again."""
    p = ctx.p
    _conv(ctx, conv_id)
    last = p.db.one("SELECT id, content, meta, created_at FROM messages WHERE conversation_id=? AND role='user'"
                    " ORDER BY created_at DESC LIMIT 1", (conv_id,))
    if not last:
        raise ApiError(400, "nothing_to_regenerate", "再生成するメッセージがありません")
    meta = loads(last["meta"], {})
    attachments = [a["id"] for a in meta.get("attachments", []) if p.files.get(ctx.uid, a["id"])]
    job = p.jobs.create(ctx.user, "chat", {"content": last["content"], "attachments": attachments, "mode": body.mode,
                                           "user_message_id": last["id"]}, conversation_id=conv_id)
    for j in list(p.jobs.jobs.values()):
        if j.conversation_id == conv_id and j.user_id == ctx.uid and j.id != job.id:
            p.jobs.cancel(j.id, ctx.uid)
    p.db.execute("DELETE FROM messages WHERE conversation_id=? AND created_at>? AND role!='user'",
                 (conv_id, last["created_at"]))
    p.db.execute("UPDATE messages SET job_id=? WHERE id=?", (job.id, last["id"]))
    p.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conv_id))
    p.jobs.start(job, lambda j: run_chat(p, j))
    return {"conversation_id": conv_id, "user_message_id": last["id"], "job": job.public()}
