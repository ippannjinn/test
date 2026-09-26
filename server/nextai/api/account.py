from __future__ import annotations

import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, require_user
from ..auth.service import SELF_EDITABLE, AuthError, public_user
from ..util import day_key

router = APIRouter(prefix="/api/account", tags=["account"])
User = Annotated[Ctx, Depends(require_user)]


class ProfileBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=1000)
    ui_prefs: dict[str, Any] | None = None


class RenameBody(BaseModel):
    name: str = Field(max_length=60)


class ApiKeyBody(BaseModel):
    name: str = Field(default="API", max_length=60)
    days: float = Field(default=90, gt=0, le=3650)


def usage(p, user: dict) -> dict:
    row = p.db.one("SELECT * FROM usage_daily WHERE user_id=? AND day=?", (user["id"], day_key())) or {}
    return {"storage_used_mb": round(p.files.usage_bytes(user["id"]) / 2**20, 1),
            "storage_quota_mb": user["storage_quota_mb"],
            "generation_used_today": round(float(row.get("generation_units", 0)), 1),
            "generation_quota_daily": user["generation_quota_daily"],
            "jobs_today": int(row.get("jobs", 0)), "tokens_today": int(row.get("tokens", 0)),
            "concurrent_jobs": user["concurrent_jobs"]}


@router.get("/profile")
def get_profile(ctx: User):
    return {"user": public_user(ctx.user), "usage": usage(ctx.p, ctx.user)}


@router.patch("/profile")
def update_profile(body: ProfileBody, ctx: User):
    try:
        u = ctx.p.auth.update_user(ctx.uid, body.model_dump(exclude_none=True), allowed=SELF_EDITABLE, actor=ctx.user, ip=ctx.ip)
    except AuthError as e:
        raise ApiError(e.status, e.code, e.message)
    return {"user": public_user(u)}


@router.post("/avatar")
def upload_avatar(ctx: User, file: UploadFile = File(...)):
    data = file.file.read(3 * 2**20 + 1)
    if len(data) > 3 * 2**20:
        raise ApiError(413, "too_large", "アイコン画像は3MBまでです")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise ApiError(400, "invalid_image", "画像を読み込めませんでした")
    side = min(img.size)
    img = img.crop(((img.width - side) // 2, (img.height - side) // 2, (img.width + side) // 2, (img.height + side) // 2))
    img = img.resize((256, 256))
    path = ctx.p.files.safe_path(ctx.uid, "avatar.png")
    img.save(path, format="PNG")
    ctx.p.db.execute("UPDATE users SET avatar_file='avatar.png' WHERE id=?", (ctx.uid,))
    return {"ok": True}


@router.get("/avatar")
def get_avatar(ctx: User):
    if not ctx.user["avatar_file"]:
        raise ApiError(404, "not_found", "アイコンが設定されていません")
    return FileResponse(ctx.p.files.safe_path(ctx.uid, "avatar.png"), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=300"})


@router.delete("/avatar")
def delete_avatar(ctx: User):
    p = ctx.p.files.safe_path(ctx.uid, "avatar.png")
    p.unlink(missing_ok=True)
    ctx.p.db.execute("UPDATE users SET avatar_file=NULL WHERE id=?", (ctx.uid,))
    return {"ok": True}


@router.get("/devices")
def devices(ctx: User):
    rows = ctx.p.auth.list_devices(ctx.uid)
    for r in rows:
        r["current"] = r["id"] == ctx.session.get("device_id")
    return {"devices": rows}


@router.patch("/devices/{device_id}")
def rename_device(device_id: str, body: RenameBody, ctx: User):
    try:
        if not ctx.p.auth.rename_device(device_id, ctx.uid, body.name):
            raise ApiError(404, "not_found", "端末が見つかりません")
    except AuthError as e:
        raise ApiError(e.status, e.code, e.message)
    return {"ok": True}


@router.delete("/devices/{device_id}")
def revoke_device(device_id: str, ctx: User):
    if not ctx.p.auth.revoke_device(device_id, "user_revoked", user_id=ctx.uid):
        raise ApiError(404, "not_found", "端末が見つかりません")
    ctx.p.audit.record("device.revoke", actor=ctx.user, target=device_id, ip=ctx.ip)
    return {"ok": True}


@router.get("/sessions")
def sessions(ctx: User):
    rows = ctx.p.auth.list_sessions(ctx.uid)
    for r in rows:
        r["current"] = r["id"] == ctx.session["id"]
    return {"sessions": rows}


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: str, ctx: User):
    if not ctx.p.auth.revoke_session(session_id, "user_revoked", user_id=ctx.uid):
        raise ApiError(404, "not_found", "セッションが見つかりません")
    return {"ok": True}


@router.post("/sessions/revoke-others")
def revoke_others(ctx: User):
    n = ctx.p.auth.revoke_user_sessions(ctx.uid, "user_revoked_others", except_session_id=ctx.session["id"])
    ctx.p.audit.record("session.revoke_others", actor=ctx.user, ip=ctx.ip, count=n)
    return {"revoked": n}


@router.get("/usage")
def get_usage(ctx: User):
    return usage(ctx.p, ctx.user)


def _no_token(ctx: Ctx) -> None:
    if ctx.auth == "token":
        raise ApiError(403, "token_forbidden", "APIキーの管理はブラウザからログインして行ってください")


@router.get("/api-keys")
def list_api_keys(ctx: User):
    _no_token(ctx)
    a = ctx.p.settings.api
    keys = [k for k in ctx.p.auth.list_api_tokens(ctx.uid) if k["scopes"] == ["openai"]]
    return {"keys": keys, "enabled": bool(a.enabled and a.member_keys), "max_days": a.key_max_days,
            "max_keys": a.max_keys_per_user}


@router.post("/api-keys")
def create_api_key(body: ApiKeyBody, ctx: User):
    _no_token(ctx)
    try:
        token, row = ctx.p.auth.create_personal_api_key(ctx.user, name=body.name, days=body.days, ip=ctx.ip)
    except AuthError as e:
        raise ApiError(e.status, e.code, e.message) from None
    row.pop("token_hash", None)
    row["scopes"] = row["scopes"].split(",")
    return {"key": token, "token": row}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: str, ctx: User):
    _no_token(ctx)
    if not ctx.p.auth.revoke_api_token(key_id, "user_revoked", actor=ctx.user, ip=ctx.ip, user_id=ctx.uid):
        raise ApiError(404, "not_found", "APIキーが見つかりません")
    return {"ok": True}
