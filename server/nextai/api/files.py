from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, require_user
from ..services.files import INLINE_MIMES, FileTooLarge, QuotaExceeded
from ..services.storage import DiskFull

router = APIRouter(prefix="/api/files", tags=["files"])
User = Annotated[Ctx, Depends(require_user)]


def public_file(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "mime": r["mime"], "size": r["size"], "kind": r["kind"],
            "created_at": r["created_at"], "job_id": r["job_id"], "meta": r.get("meta") or {}}


@router.post("")
def upload(request: Request, ctx: User, file: UploadFile = File(...)):
    p = ctx.p
    try:
        p.storage.ensure_can_write(int(request.headers.get("content-length", "0") or 0), "アップロード")
        row = p.files.save_stream(ctx.user, file.file, file.filename or "file", kind="upload")
    except (QuotaExceeded, DiskFull) as e:
        raise ApiError(507, "quota_exceeded", str(e))
    except FileTooLarge as e:
        raise ApiError(413, "too_large", str(e))
    row["meta"] = {}
    return {"file": public_file(row)}


@router.get("")
def list_files(ctx: User, kind: str | None = None, limit: int = 200, offset: int = 0):
    if kind and kind not in ("upload", "generated", "workspace"):
        raise ApiError(400, "invalid_kind", "kind が不正です")
    return {"files": [public_file(r) for r in ctx.p.files.list(ctx.uid, kind, limit, offset)],
            "used_mb": round(ctx.p.files.usage_bytes(ctx.uid) / 2**20, 1), "quota_mb": ctx.user["storage_quota_mb"]}


@router.get("/{file_id}")
def get_file(file_id: str, ctx: User):
    row = ctx.p.files.get(ctx.uid, file_id)
    if not row:
        raise ApiError(404, "not_found", "ファイルが見つかりません")
    return {"file": public_file(row)}


@router.get("/{file_id}/content")
def file_content(file_id: str, ctx: User, download: int = 0):
    row = ctx.p.files.get(ctx.uid, file_id)
    if not row:
        raise ApiError(404, "not_found", "ファイルが見つかりません")
    path = ctx.p.files.path_of(row)
    if not path.exists():
        raise ApiError(410, "gone", "ファイルが存在しません")
    inline = row["mime"] in INLINE_MIMES and not download
    media = row["mime"] if inline else "application/octet-stream"
    disp = "inline" if inline else "attachment"
    return FileResponse(path, media_type=media, headers={
        "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(row['name'])}",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, max-age=3600"})


TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml", ".yml",
            ".toml", ".ini", ".log", ".sql", ".sh", ".bat", ".ps1", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".rb",
            ".php", ".svg", ".tex"}
# Rendered previews run in an opaque-origin sandbox: scripts may run, but they cannot reach the app, cookies or
# the API (no same-origin, no connect-src), and the page may only be framed by NextAI itself.
PREVIEW_CSP = ("sandbox allow-scripts allow-popups allow-modals; default-src 'none'; script-src 'unsafe-inline' https:; "
               "style-src 'unsafe-inline' https:; img-src data: blob: https:; font-src data: https:; media-src data: blob:; "
               "frame-ancestors 'self'")
PREVIEW_HEADERS = {"Content-Security-Policy": PREVIEW_CSP, "X-Frame-Options": "SAMEORIGIN", "X-Content-Type-Options": "nosniff",
                   "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _is_text(row: dict) -> bool:
    return row["mime"].startswith("text/") or row["mime"] in ("application/json", "image/svg+xml") or \
        Path(row["name"]).suffix.lower() in TEXT_EXT


@router.get("/{file_id}/text")
def file_text(file_id: str, ctx: User, max_bytes: int = 200_000):
    """First part of a text-like file for inline previews (CSV tables, code, markdown)."""
    row = ctx.p.files.get(ctx.uid, file_id)
    if not row:
        raise ApiError(404, "not_found", "ファイルが見つかりません")
    if not _is_text(row):
        raise ApiError(415, "not_text", "テキストとしてプレビューできないファイルです")
    path = ctx.p.files.path_of(row)
    with open(path, "rb") as fh:
        data = fh.read(min(max(1000, max_bytes), 1_000_000) + 1)
    limit = min(max(1000, max_bytes), 1_000_000)
    return {"name": row["name"], "mime": row["mime"], "size": row["size"], "truncated": len(data) > limit,
            "text": data[:limit].decode("utf-8", "replace")}


@router.get("/{file_id}/preview")
def file_preview(file_id: str, ctx: User):
    """Renders an HTML / SVG result inside a locked-down sandbox (for the in-chat preview frame)."""
    row = ctx.p.files.get(ctx.uid, file_id)
    if not row:
        raise ApiError(404, "not_found", "ファイルが見つかりません")
    ext = Path(row["name"]).suffix.lower()
    if ext not in (".html", ".htm", ".svg") or row["size"] > 5 * 2**20:
        raise ApiError(415, "not_previewable", "このファイルはプレビューできません")
    body = ctx.p.files.path_of(row).read_bytes()
    media = "image/svg+xml" if ext == ".svg" else "text/html; charset=utf-8"
    return Response(body, media_type=media, headers=PREVIEW_HEADERS)


@router.delete("/{file_id}")
def delete_file(file_id: str, ctx: User):
    if not ctx.p.files.delete(ctx.uid, file_id):
        raise ApiError(404, "not_found", "ファイルが見つかりません")
    return {"ok": True}


# ------------------------------------------------------------------ previews of HTML / SVG code blocks
preview_router = APIRouter(prefix="/api/preview", tags=["files"])
_PREVIEWS: dict[str, tuple[str, float, str, str]] = {}  # token -> (user_id, expires, kind, content)


class PreviewBody(BaseModel):
    kind: Literal["html", "svg"] = "html"
    content: str = Field(min_length=1, max_length=2_000_000)


@preview_router.post("")
def create_preview(body: PreviewBody, ctx: User):
    now_ = time.time()
    for k in [k for k, v in _PREVIEWS.items() if v[1] < now_]:
        _PREVIEWS.pop(k, None)
    mine = sorted((v[1], k) for k, v in _PREVIEWS.items() if v[0] == ctx.uid)
    for _, k in mine[:max(0, len(mine) - 49)]:
        _PREVIEWS.pop(k, None)
    token = secrets.token_urlsafe(18)
    _PREVIEWS[token] = (ctx.uid, now_ + 3600, body.kind, body.content)
    return {"url": f"/api/preview/{token}"}


@preview_router.get("/{token}")
def get_preview(token: str, ctx: User):
    item = _PREVIEWS.get(token)
    if not item or item[0] != ctx.uid or item[1] < time.time():
        raise ApiError(404, "not_found", "プレビューの有効期限が切れました")
    media = "image/svg+xml" if item[2] == "svg" else "text/html; charset=utf-8"
    return Response(item[3].encode("utf-8"), media_type=media, headers=PREVIEW_HEADERS)
