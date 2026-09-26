"""Admin API — reachable only with an admin_app bearer session from the server PC (by default)."""
from __future__ import annotations

import asyncio
import json
import platform as pyplatform
import sys
import threading
import time
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, require_admin
from ..auth.service import ADMIN_EDITABLE, AuthError, public_user
from ..backup import create_backup, list_backups, schedule_restore
from ..diagnostics import run_full
from ..install.models import install_models, remove_model_files
from ..models.catalog import parse_spec
from ..models.manager import HOT
from ..netinfo import connection_urls
from ..security.tls import fingerprint_sha256
from ..util import dumps, loads, now

router = APIRouter(prefix="/api/admin", tags=["admin"])
Admin = Annotated[Ctx, Depends(require_admin)]


def _ae(e: AuthError) -> ApiError:
    return ApiError(e.status, e.code, e.message)


def _urls(p) -> list[dict]:
    s = p.settings.server
    return connection_urls(s.port, s.tls, s.public_url)


def invitation_text(p, user: dict, password: str | None) -> str:
    urls = _urls(p)
    main = next((u["url"] for u in urls if u["kind"] in ("public", "lan", "tailscale")), urls[0]["url"])
    lines = [f"【{p.settings.server.name} へのご招待】", "", f"接続URL: {main}"]
    extra = [u for u in urls if u["url"] != main and u["kind"] in ("lan", "tailscale", "public")]
    for u in extra[:3]:
        lines.append(f"  (別経路: {u['url']} - {u['label']})")
    lines += [f"ユーザー名: {user['username']}"]
    if password:
        lines.append(f"初期パスワード: {password}")
    lines += ["", "初回ログイン後、パスワードの変更をお願いします。",
              "「この端末を信頼する」にチェックすると、次回から再ログインが不要になります。"]
    if p.settings.server.tls and not p.settings.server.cert_file:
        ca = p.settings.paths.certs / "ca.crt"
        if ca.exists():
            lines += ["", "ブラウザに証明書の警告が出る場合は、以下からCA証明書をインストールしてください:",
                      f"{main.rstrip('/')}/ca.crt", f"(SHA-256: {fingerprint_sha256(ca)[:47]}…)"]
    return "\n".join(lines)


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
def dashboard(ctx: Admin):
    p = ctx.p
    snap = p.monitor.latest
    hot = [{"id": m, "name": rt.spec.display_name, "kind": rt.spec.kind, "in_use": rt.in_use,
            "vram_mb": rt.plan.est_vram_mb if rt.plan else 0, "notes": rt.plan.notes if rt.plan else []}
           for m, rt in p.models.runtimes.items() if rt.state in (HOT, "loading")]
    active = [{"id": j.id, "user_id": j.user_id, "kind": j.kind, "status": j.status, "created_at": j.created_at,
               "model": j.profile.get("model_id"), "label": j.profile.get("label")}
              for j in p.jobs.jobs.values() if j.status in ("queued", "running")]
    names = {u["id"]: u["username"] for u in p.auth.list_users()}
    for a in active:
        a["username"] = names.get(a["user_id"], "?")
    ts = now()
    online = int(p.db.scalar("SELECT COUNT(DISTINCT user_id) FROM sessions WHERE revoked_at IS NULL AND last_seen_at>?",
                             (ts - 900,)) or 0)
    return {
        "server": {"name": p.settings.server.name, "version": p.version, "uptime_seconds": int(ts - p.started_at),
                   "backend_mode": p.backends.mode, "gpu_provider": p.gpu.name, "sandbox": p.sandbox.name,
                   "tls": p.settings.server.tls, "port": p.settings.server.port, "urls": _urls(p)},
        "resources": snap.to_dict(), "governor": p.governor.state.to_dict(), "queue": p.scheduler.stats(),
        "models_loaded": hot, "active_jobs": active, "errors": list(p.errors.records)[-30:][::-1],
        "swaps": list(p.models.swap_log)[-10:][::-1],
        "users": {"total": len(names), "online_15min": online},
        "status": "degraded" if p.governor.state.level.value >= 2 else "ok",
    }


@router.get("/metrics")
def metrics(ctx: Admin, n: int = 300):
    out = []
    for s in ctx.p.monitor.recent(min(max(n, 10), 900)):
        g = s.gpu
        out.append({"ts": s.ts, "cpu": s.cpu_percent, "ram_available_mb": s.ram_available_mb, "ram_total_mb": s.ram_total_mb,
                    "vram_used_mb": g.vram_used_mb if g else 0, "vram_total_mb": g.vram_total_mb if g else 0,
                    "gpu_util": g.util_percent if g else 0, "gpu_temp": g.temp_c if g else None,
                    "own_vram_mb": s.own_vram_mb, "disk_free_gb": s.disk_free_gb})
    return {"samples": out}


# ------------------------------------------------------------------ users
class CreateUserBody(BaseModel):
    username: str = Field(max_length=32)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    role: Literal["admin", "member"] = "member"
    must_change_password: bool = True
    bio: str | None = Field(default=None, max_length=1000)
    storage_quota_mb: int | None = None
    generation_quota_daily: int | None = None
    concurrent_jobs: int | None = None
    queue_priority: int | None = None
    rate_limit_per_min: int | None = None


class UpdateUserBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=1000)
    role: Literal["admin", "member"] | None = None
    storage_quota_mb: int | None = None
    generation_quota_daily: int | None = None
    concurrent_jobs: int | None = None
    queue_priority: int | None = None
    rate_limit_per_min: int | None = None


class StateBody(BaseModel):
    state: Literal["active", "suspended", "disabled"]


class ResetBody(BaseModel):
    password: str | None = Field(default=None, max_length=256)
    must_change: bool = True


class DeleteBody(BaseModel):
    confirm_username: str


def _user_row(p, u: dict) -> dict:
    d = public_user(u)
    ts = now()
    d["storage_used_mb"] = round(p.files.usage_bytes(u["id"]) / 2**20, 1) if u["state"] != "deleted" else 0
    d["active_sessions"] = int(p.db.scalar("SELECT COUNT(*) FROM sessions WHERE user_id=? AND revoked_at IS NULL AND"
                                           " idle_expires_at>?", (u["id"], ts)) or 0)
    d["trusted_devices"] = int(p.db.scalar("SELECT COUNT(*) FROM devices WHERE user_id=? AND revoked_at IS NULL AND"
                                           " expires_at>?", (u["id"], ts)) or 0)
    usage = p.db.one("SELECT generation_units, jobs FROM usage_daily WHERE user_id=? AND day=date('now','localtime')",
                     (u["id"],)) or {}
    d["generation_used_today"] = usage.get("generation_units", 0)
    d["jobs_today"] = usage.get("jobs", 0)
    return d


def _get_user(p, user_id: str) -> dict:
    u = p.auth.get_user(user_id)
    if not u or u["state"] == "deleted":
        raise ApiError(404, "not_found", "ユーザーが見つかりません")
    return u


@router.get("/users")
def list_users(ctx: Admin):
    return {"users": [_user_row(ctx.p, u) for u in ctx.p.auth.list_users()]}


@router.post("/users")
def create_user(body: CreateUserBody, ctx: Admin):
    p = ctx.p
    fields = body.model_dump(exclude={"username", "password", "display_name", "role", "must_change_password"})
    try:
        u, pw = p.auth.create_user(username=body.username, password=body.password, role=body.role,
                                   display_name=body.display_name, must_change_password=body.must_change_password,
                                   actor=ctx.user, ip=ctx.ip, **fields)
    except AuthError as e:
        raise _ae(e)
    return {"user": _user_row(p, u), "initial_password": pw, "invitation": invitation_text(p, u, pw)}


@router.get("/users/{user_id}")
def get_user(user_id: str, ctx: Admin):
    p = ctx.p
    u = _get_user(p, user_id)
    return {"user": _user_row(p, u), "sessions": p.auth.list_sessions(user_id), "devices": p.auth.list_devices(user_id)}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserBody, ctx: Admin):
    try:
        u = ctx.p.auth.update_user(user_id, body.model_dump(exclude_none=True), allowed=ADMIN_EDITABLE,
                                   actor=ctx.user, ip=ctx.ip)
    except AuthError as e:
        raise _ae(e)
    return {"user": _user_row(ctx.p, u)}


@router.post("/users/{user_id}/state")
async def set_state(user_id: str, body: StateBody, ctx: Admin):
    try:
        u = ctx.p.auth.set_state(user_id, body.state, actor=ctx.user, ip=ctx.ip)
    except AuthError as e:
        raise _ae(e)
    if body.state != "active":
        for j in list(ctx.p.jobs.jobs.values()):
            if j.user_id == user_id:
                ctx.p.jobs.cancel(j.id)
    return {"user": _user_row(ctx.p, u)}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: str, body: ResetBody, ctx: Admin):
    try:
        pw = ctx.p.auth.reset_password(user_id, body.password, must_change=body.must_change, actor=ctx.user, ip=ctx.ip)
    except AuthError as e:
        raise _ae(e)
    u = ctx.p.auth.get_user(user_id)
    return {"password": pw, "invitation": invitation_text(ctx.p, u, pw)}


@router.post("/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: str, ctx: Admin):
    _get_user(ctx.p, user_id)
    n = ctx.p.auth.revoke_user_sessions(user_id, "admin_revoked",
                                        except_session_id=ctx.session["id"] if user_id == ctx.uid else None)
    ctx.p.audit.record("admin.revoke_sessions", actor=ctx.user, target=user_id, ip=ctx.ip, count=n)
    return {"revoked": n}


@router.get("/users/{user_id}/devices")
def user_devices(user_id: str, ctx: Admin):
    _get_user(ctx.p, user_id)
    return {"devices": ctx.p.auth.list_devices(user_id)}


@router.delete("/users/{user_id}/devices/{device_id}")
def revoke_user_device(user_id: str, device_id: str, ctx: Admin):
    if not ctx.p.auth.revoke_device(device_id, "admin_revoked", user_id=user_id):
        raise ApiError(404, "not_found", "端末が見つかりません")
    ctx.p.audit.record("admin.revoke_device", actor=ctx.user, target=device_id, ip=ctx.ip, user_id=user_id)
    return {"ok": True}


@router.post("/users/{user_id}/revoke-devices")
def revoke_user_devices(user_id: str, ctx: Admin):
    _get_user(ctx.p, user_id)
    n = ctx.p.auth.revoke_user_devices(user_id, "admin_revoked")
    ctx.p.audit.record("admin.revoke_devices", actor=ctx.user, target=user_id, ip=ctx.ip, count=n)
    return {"revoked": n}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, body: DeleteBody, ctx: Admin):
    p = ctx.p
    u = _get_user(p, user_id)
    if body.confirm_username != u["username"]:
        raise ApiError(400, "confirm_mismatch", "確認用のユーザー名が一致しません")
    if u["state"] != "disabled":
        raise ApiError(409, "not_disabled", "完全削除の前にアカウントを「無効化」してください (無効化 → 保持 → 完全削除)")
    if user_id == ctx.uid:
        raise ApiError(409, "self_delete", "自分自身は削除できません")
    for j in list(p.jobs.jobs.values()):
        if j.user_id == user_id:
            p.jobs.cancel(j.id)
    await asyncio.to_thread(p.files.delete_all, user_id)
    p.auth.mark_deleted(user_id, actor=ctx.user, ip=ctx.ip)
    return {"ok": True}


@router.get("/users/{user_id}/avatar")
def user_avatar(user_id: str, ctx: Admin):
    u = _get_user(ctx.p, user_id)
    if not u["avatar_file"]:
        raise ApiError(404, "not_found", "アイコンがありません")
    return FileResponse(ctx.p.files.safe_path(user_id, "avatar.png"), media_type="image/png")


@router.get("/users/{user_id}/invitation")
def user_invitation(user_id: str, ctx: Admin):
    return {"invitation": invitation_text(ctx.p, _get_user(ctx.p, user_id), None)}


class AgentBody(BaseModel):
    days: float = Field(default=7, gt=0, le=90)
    debug: bool = True


def agent_env(p, res: dict) -> str:
    s = p.settings.server
    return "\n".join([
        "# NextAI Platform - Claude 専用アカウント (Claude Code 用)",
        "# このファイルは秘密情報です。共有・コミットしないでください。",
        f"NEXTAI_URL={'https' if s.tls else 'http'}://127.0.0.1:{s.port}",
        "NEXTAI_CA=C:\\Program Files\\NextAI\\ca.crt",
        f"NEXTAI_TOKEN={res['token']}",
        f"NEXTAI_TOKEN_SCOPES={','.join(res['scopes'])}",
        f"NEXTAI_UI_USER={res['username']}",
        f"NEXTAI_UI_PASSWORD={res['password']}",
        f"NEXTAI_TOKEN_EXPIRES={iso_date(res['expires_at'])}",
        "",
    ])


def iso_date(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@router.post("/agent-account")
def create_agent_account(body: AgentBody, ctx: Admin):
    try:
        res = ctx.p.auth.ensure_agent_account(days=body.days, debug=body.debug, actor=ctx.user, ip=ctx.ip)
    except AuthError as e:
        raise _ae(e)
    return {**res, "env": agent_env(ctx.p, res)}


@router.get("/tokens")
def list_tokens(ctx: Admin):
    return {"tokens": ctx.p.auth.list_api_tokens()}


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: str, ctx: Admin):
    if not ctx.p.auth.revoke_api_token(token_id, "admin_revoked", actor=ctx.user, ip=ctx.ip):
        raise ApiError(404, "not_found", "有効なトークンが見つかりません")
    return {"ok": True}


@router.post("/sessions/revoke-all")
def revoke_all(ctx: Admin):
    n = ctx.p.auth.revoke_all_sessions("admin_revoked_all", except_session_id=ctx.session["id"])
    ctx.p.audit.record("admin.revoke_all_sessions", actor=ctx.user, ip=ctx.ip, count=n)
    return {"revoked": n}


# ------------------------------------------------------------------ models
class CustomModelBody(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._\-]{2,63}$")
    display_name: str = Field(max_length=100)
    kind: Literal["llm", "vlm", "embedding"]
    repo: str = Field(pattern=r"^[\w.\-]+/[\w.\-]+$")
    pattern: str = Field(max_length=200)
    mmproj_pattern: str | None = Field(default=None, max_length=200)
    size_gb: float = Field(gt=0, lt=200)
    roles: list[str] = Field(default_factory=lambda: ["chat"])
    moe: bool = False
    n_layers: int = Field(default=32, ge=1, le=200)
    n_kv_heads: int = Field(default=8, ge=1, le=128)
    head_dim: int = Field(default=128, ge=16, le=512)
    ctx: int = Field(default=16384, ge=2048, le=262144)
    capabilities: dict[str, float] = Field(default_factory=dict)
    license: str = "unknown"


def _model_status(p) -> list[dict]:
    rows = p.models.status()
    for r in rows:
        r["download"] = p.downloads.get(r["id"])
    return rows


@router.get("/models")
def list_models(ctx: Admin):
    return {"models": _model_status(ctx.p), "backends": ctx.p.backends.status(), "swaps": list(ctx.p.models.swap_log)[::-1],
            "thrashing": ctx.p.models.thrashing()}


@router.get("/model-sets")
def model_sets(ctx: Admin):
    p = ctx.p
    s = p.monitor.latest
    vram = (s.gpu.vram_total_mb / 1024) if s.gpu else 0
    return p.catalog.select_set(vram_gb=vram, ram_gb=s.ram_total_mb / 1024, disk_free_gb=s.disk_free_gb)


def _model_or_404(p, model_id: str):
    if model_id not in p.catalog.models:
        raise ApiError(404, "not_found", "モデルが見つかりません")
    return p.catalog.get(model_id)


@router.post("/models/{model_id}/enable")
def enable_model(model_id: str, ctx: Admin):
    _model_or_404(ctx.p, model_id)
    ctx.p.models.set_enabled(model_id, True)
    ctx.p.audit.record("model.enable", actor=ctx.user, target=model_id, ip=ctx.ip)
    return {"ok": True}


@router.post("/models/{model_id}/disable")
async def disable_model(model_id: str, ctx: Admin):
    _model_or_404(ctx.p, model_id)
    ctx.p.models.set_enabled(model_id, False)
    rt = ctx.p.models.runtimes[model_id]
    if rt.state == HOT and rt.in_use == 0:
        await ctx.p.models.unload(model_id, "disabled by admin")
    ctx.p.audit.record("model.disable", actor=ctx.user, target=model_id, ip=ctx.ip)
    return {"ok": True}


@router.post("/models/{model_id}/load")
async def load_model(model_id: str, ctx: Admin):
    p = ctx.p
    _model_or_404(p, model_id)
    if not p.models.usable(model_id):
        raise ApiError(409, "unusable", "このモデルは利用できません (未インストール・無効・ランタイム無し)")
    status, ids, plan = p.models.can_load(model_id)
    if status != "ok":
        raise ApiError(409, "cannot_load", "使用中のモデルがあるため、またはVRAM不足のため今はロードできません")
    p.models.reserve_load(model_id, ids)
    try:
        await p.models.load(model_id, ids, plan, reason="admin")
    except Exception as e:  # noqa: BLE001
        raise ApiError(500, "load_failed", str(e)[:500])
    p.audit.record("model.load", actor=ctx.user, target=model_id, ip=ctx.ip)
    return {"ok": True, "plan": plan.to_dict()}


@router.post("/models/{model_id}/unload")
async def unload_model(model_id: str, ctx: Admin):
    p = ctx.p
    _model_or_404(p, model_id)
    rt = p.models.runtimes[model_id]
    if rt.in_use > 0:
        raise ApiError(409, "in_use", "実行中のジョブがあるためアンロードできません")
    await p.models.unload(model_id, "admin")
    p.audit.record("model.unload", actor=ctx.user, target=model_id, ip=ctx.ip)
    return {"ok": True}


@router.post("/models/{model_id}/download")
def download_model(model_id: str, ctx: Admin):
    p = ctx.p
    _model_or_404(p, model_id)
    cur = p.downloads.get(model_id)
    if cur and cur.get("state") == "running":
        raise ApiError(409, "running", "ダウンロード中です")
    if p.governor.state.disk != "ok":
        raise ApiError(507, "disk_low", "ディスク容量が少ないため新しいモデルの取得を停止しています")
    cancel = threading.Event()
    state: dict[str, Any] = {"state": "running", "done": 0, "total": 0, "started_at": now(), "cancel": cancel, "error": None}
    p.downloads[model_id] = state

    def emit(ev: dict) -> None:
        if ev.get("event") == "progress":
            state["done"], state["total"] = ev.get("overall_done", 0), ev.get("overall_total", 0)
            state["file"] = ev.get("file")
        elif ev.get("event") == "model_error":
            state["error"] = ev.get("error")

    def work() -> None:
        try:
            res = install_models(p.settings, p.db, p.catalog, [model_id], emit, cancel)
            state["state"] = "done" if res.get(model_id) == "ok" else "error"
            state["error"] = state["error"] or (None if res.get(model_id) == "ok" else res.get(model_id))
        except Exception as e:  # noqa: BLE001
            state["state"], state["error"] = "error", str(e)[:500]
        state["finished_at"] = now()

    threading.Thread(target=work, daemon=True, name=f"download-{model_id}").start()
    p.audit.record("model.download", actor=ctx.user, target=model_id, ip=ctx.ip)
    return {"ok": True}


@router.post("/models/{model_id}/download/cancel")
def cancel_download(model_id: str, ctx: Admin):
    st = ctx.p.downloads.get(model_id)
    if not st or st.get("state") != "running":
        raise ApiError(404, "not_running", "ダウンロードは実行されていません")
    st["cancel"].set()
    return {"ok": True}


@router.get("/downloads")
def downloads(ctx: Admin):
    return {"downloads": {k: {kk: vv for kk, vv in v.items() if kk != "cancel"} for k, v in ctx.p.downloads.items()}}


class ConfirmBody(BaseModel):
    confirm: str


@router.delete("/models/{model_id}/files")
async def delete_model_files(model_id: str, body: ConfirmBody, ctx: Admin):
    p = ctx.p
    _model_or_404(p, model_id)
    if body.confirm != model_id:
        raise ApiError(400, "confirm_mismatch", "確認用のモデルIDが一致しません")
    rt = p.models.runtimes[model_id]
    if rt.in_use > 0:
        raise ApiError(409, "in_use", "使用中のモデルは削除できません")
    await p.models.unload(model_id, "files removed")
    await asyncio.to_thread(remove_model_files, p.settings, p.db, model_id)
    p.audit.record("model.delete_files", actor=ctx.user, target=model_id, ip=ctx.ip)
    return {"ok": True}


@router.post("/models/custom")
def add_custom_model(body: CustomModelBody, ctx: Admin):
    p = ctx.p
    if body.id in p.catalog.models and not p.catalog.models[body.id].custom:
        raise ApiError(409, "exists", "同じIDの標準モデルがあります")
    comps = {"model": [{"repo": body.repo, "patterns": [body.pattern], "exclude": ["mmproj*"]}]}
    if body.mmproj_pattern:
        comps["mmproj"] = [{"repo": body.repo, "patterns": [body.mmproj_pattern]}]
    raw = {"id": body.id, "display_name": body.display_name, "kind": body.kind, "roles": body.roles,
           "backend": "llamacpp", "license": body.license, "size_gb": body.size_gb, "components": comps,
           "arch": {"n_layers": body.n_layers, "n_kv_heads": body.n_kv_heads, "head_dim": body.head_dim,
                    "moe": body.moe, "expert_fraction": 0.92 if body.moe else 0.0},
           "capabilities": body.capabilities or {"chat": 0.6, "coding": 0.5, "reasoning": 0.5, "tools": 0.5,
                                                 "japanese": 0.5, "writing": 0.5},
           "ctx_max": body.ctx, "defaults": {"ctx": min(body.ctx, 32768), "parallel": 2},
           "speed": 0.6}
    try:
        spec = parse_spec(raw, custom=True)
    except (KeyError, ValueError) as e:
        raise ApiError(400, "invalid_spec", str(e))
    p.models.add_custom_spec(spec, raw)
    p.audit.record("model.add_custom", actor=ctx.user, target=body.id, ip=ctx.ip, repo=body.repo)
    return {"ok": True}


# ------------------------------------------------------------------ workers
@router.get("/workers")
def workers(ctx: Admin):
    p = ctx.p
    b = p.backends.status()
    rts = p.models.runtimes

    def kind_state(kinds: tuple[str, ...]) -> dict:
        items = [rt for rt in rts.values() if rt.spec.kind in kinds]
        return {"loaded": [rt.spec.id for rt in items if rt.state == HOT],
                "busy": sum(rt.in_use for rt in items),
                "installed": [rt.spec.id for rt in items if p.models.usable(rt.spec.id)]}

    running = p.scheduler.running.values()
    return {"workers": [
        {"id": "llm", "name": "General / Coding LLM", "backend": b["llm"]["name"], "available": b["llm"]["available"],
         **kind_state(("llm",))},
        {"id": "vision", "name": "Vision / OCR", "backend": b["llm"]["name"], "available": b["llm"]["available"],
         **kind_state(("vlm",))},
        {"id": "embedding", "name": "Embedding (CPU)", "backend": b["llm"]["name"], "available": b["llm"]["available"],
         **kind_state(("embedding",))},
        {"id": "image", "name": "Image", "backend": b["image"]["name"], "available": b["image"]["available"],
         **kind_state(("image",))},
        {"id": "video", "name": "Video", "backend": b["video"]["name"], "available": b["video"]["available"],
         **kind_state(("video",))},
        {"id": "music", "name": "Music", "backend": b["music"]["name"], "available": b["music"]["available"],
         **kind_state(("music",))},
        {"id": "sandbox", "name": "Code Sandbox", "backend": p.sandbox.name, "available": p.sandbox.available,
         "loaded": [], "busy": 0, "installed": []},
        {"id": "web", "name": "Web / Browser", "backend": p.settings.web.search_provider, "available": True,
         "loaded": [], "busy": 0, "installed": []},
    ], "running_units": len(list(running))}


@router.post("/workers/{worker_id}/restart")
async def restart_worker(worker_id: str, ctx: Admin):
    p = ctx.p
    kinds = {"llm": ("llm",), "vision": ("vlm",), "embedding": ("embedding",), "image": ("image",),
             "video": ("video",), "music": ("music",)}.get(worker_id)
    if kinds is None:
        raise ApiError(404, "not_found", "ワーカーが見つかりません")
    stopped = []
    for mid, rt in p.models.runtimes.items():
        if rt.spec.kind in kinds and rt.state in (HOT, "error") and rt.in_use == 0:
            await p.models.unload(mid, "worker restart")
            stopped.append(mid)
    p.audit.record("worker.restart", actor=ctx.user, target=worker_id, ip=ctx.ip, stopped=stopped)
    return {"stopped": stopped, "note": "次のリクエストで自動的に再ロードされます"}


# ------------------------------------------------------------------ settings
class SettingsBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)


@router.get("/settings")
def get_settings(ctx: Admin):
    return {"settings": ctx.p.settings.describe()}


@router.put("/settings")
def put_settings(body: SettingsBody, ctx: Admin):
    p = ctx.p
    errors, restart = {}, False
    coerced = {}
    for k, v in body.values.items():
        try:
            coerced[k] = p.settings.validate_override(k, v)
        except (KeyError, ValueError, TypeError) as e:
            errors[k] = str(e) or "invalid"
    if errors:
        raise ApiError(400, "invalid_settings", "; ".join(f"{k}: {v}" for k, v in errors.items()))
    for k, v in coerced.items():
        p.db.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at,"
                     " updated_by=excluded.updated_by", (k, dumps(v), now(), ctx.user["username"]))
        restart |= k.startswith(("server.", "models.backend_mode", "models.base_port", "resources.gpu_provider"))
    for k in body.reset:
        p.db.execute("DELETE FROM settings WHERE key=?", (k,))
    p.reload_setting_overrides()
    p.scheduler.wake()
    p.audit.record("settings.update", actor=ctx.user, ip=ctx.ip,
                   keys=[k for k in coerced if "key" not in k and "token" not in k] + [f"reset:{k}" for k in body.reset])
    return {"ok": True, "restart_required": restart, "settings": p.settings.describe()}


# ------------------------------------------------------------------ queue / jobs
@router.get("/queue")
def queue(ctx: Admin):
    p = ctx.p
    names = {u["id"]: u["username"] for u in p.auth.list_users()}
    rows = p.scheduler.snapshot()
    for r in rows:
        r["username"] = names.get(r["user_id"], "?")
    return {"units": rows, "stats": p.scheduler.stats()}


@router.post("/jobs/{job_id}/cancel")
def admin_cancel(job_id: str, ctx: Admin):
    if not ctx.p.jobs.cancel(job_id):
        raise ApiError(404, "not_active", "実行中のジョブではありません")
    ctx.p.audit.record("admin.cancel_job", actor=ctx.user, target=job_id, ip=ctx.ip)
    return {"ok": True}


@router.get("/jobs")
def recent_jobs(ctx: Admin, limit: int = 100):
    rows = ctx.p.db.query("SELECT j.id, j.kind, j.status, j.created_at, j.finished_at, j.error, j.gpu_seconds, u.username"
                          " FROM jobs j LEFT JOIN users u ON u.id=j.user_id ORDER BY j.created_at DESC LIMIT ?",
                          (min(max(limit, 1), 1000),))
    return {"jobs": rows}


# ------------------------------------------------------------------ logs / audit
@router.get("/logs/list")
def logs_list(ctx: Admin):
    d = ctx.p.settings.paths.logs
    return {"logs": [{"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime}
                     for f in sorted(d.glob("*.log*")) if f.is_file()]}


@router.get("/logs")
def logs(ctx: Admin, name: str = "server.log", lines: int = 400):
    d = ctx.p.settings.paths.logs
    allowed = {f.name for f in d.glob("*.log*")}
    if name not in allowed:
        raise ApiError(404, "not_found", "ログが見つかりません")
    path = d / name
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - 2 * 2**20))
        text = f.read().decode("utf-8", "replace")
    return {"name": name, "lines": text.splitlines()[-min(max(lines, 10), 5000):]}


@router.get("/audit")
def audit(ctx: Admin, limit: int = 200, offset: int = 0, action: str | None = None, actor: str | None = None):
    return {"entries": ctx.p.audit.query(limit=limit, offset=offset, action=action, actor=actor)}


# ------------------------------------------------------------------ backup / restore
class BackupBody(BaseModel):
    include_user_files: bool = True


class RestoreBody(BaseModel):
    name: str
    confirm: str


@router.post("/backup")
async def backup(body: BackupBody, ctx: Admin):
    p = ctx.p
    p.storage.ensure_can_write(0, "バックアップ")
    path = await asyncio.to_thread(create_backup, p.settings, p.db, body.include_user_files)
    p.audit.record("admin.backup", actor=ctx.user, ip=ctx.ip, name=path.name)
    return {"name": path.name, "size": path.stat().st_size}


@router.get("/backups")
def backups(ctx: Admin):
    return {"backups": list_backups(ctx.p.settings)}


@router.post("/restore")
def restore(body: RestoreBody, ctx: Admin):
    if body.confirm != "RESTORE":
        raise ApiError(400, "confirm_required", "確認のため RESTORE と入力してください")
    try:
        schedule_restore(ctx.p.settings, body.name)
    except (FileNotFoundError, ValueError) as e:
        raise ApiError(400, "invalid_backup", str(e))
    ctx.p.audit.record("admin.restore_scheduled", actor=ctx.user, ip=ctx.ip, name=body.name)
    asyncio.get_event_loop().call_later(1.0, ctx.p.request_restart)
    return {"ok": True, "note": "サーバーを再起動して復元します"}


# ------------------------------------------------------------------ diagnostics / server
class DiagBody(BaseModel):
    full: bool = False


@router.post("/diagnostics/run")
async def run_diag(body: DiagBody, ctx: Admin):
    p = ctx.p
    job = p.jobs.create(ctx.user, "diagnostics", {"full": body.full})
    p.jobs.start(job, lambda j: run_full(p, j, body.full))
    p.audit.record("admin.diagnostics", actor=ctx.user, ip=ctx.ip, full=body.full)
    return {"job": job.public()}


@router.get("/diagnostics/latest")
def diag_latest(ctx: Admin):
    f = ctx.p.settings.paths.diagnostics / "latest.json"
    if not f.exists():
        raise ApiError(404, "not_found", "診断結果がありません")
    return json.loads(f.read_text(encoding="utf-8"))


@router.get("/jobs/{job_id}")
def admin_job(job_id: str, ctx: Admin):
    rec = ctx.p.jobs.get_record(job_id)
    if not rec:
        raise ApiError(404, "not_found", "ジョブが見つかりません")
    return {"job": rec}


@router.post("/server/restart")
def server_restart(ctx: Admin):
    ctx.p.audit.record("admin.server_restart", actor=ctx.user, ip=ctx.ip)
    asyncio.get_event_loop().call_later(0.5, ctx.p.request_restart)
    return {"ok": True}


@router.get("/server/info")
def server_info(ctx: Admin):
    p = ctx.p
    ca = p.settings.paths.certs / "ca.crt"
    return {"version": p.version, "python": sys.version.split()[0], "platform": pyplatform.platform(),
            "data_dir": str(p.settings.paths.data_dir), "uptime_seconds": int(now() - p.started_at),
            "urls": _urls(p), "ca_fingerprint": fingerprint_sha256(ca) if ca.exists() else None,
            "backend_mode": p.backends.mode, "backends": p.backends.status(), "sandbox": p.sandbox.name,
            "gpu_provider": p.gpu.name}


@router.post("/cleanup")
async def cleanup(ctx: Admin):
    removed = await asyncio.to_thread(ctx.p.storage.cleanup, True)
    return {"removed": removed}


@router.get("/update/check")
async def update_check(ctx: Admin):
    url = ctx.p.settings.server.update_manifest_url
    if not url:
        return {"configured": False, "current": ctx.p.version}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            m = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ApiError(502, "update_check_failed", f"更新情報を取得できません: {e}")
    latest = str(m.get("version", ""))
    newer = tuple(int(x) for x in latest.split(".") if x.isdigit()) > tuple(int(x) for x in ctx.p.version.split("."))
    return {"configured": True, "current": ctx.p.version, "latest": latest, "update_available": newer,
            "download_url": m.get("url"), "sha256": m.get("sha256"), "notes": m.get("notes", "")}


def _loads(v):
    return loads(v, v)
