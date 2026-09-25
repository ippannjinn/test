from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse

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


@router.delete("/{file_id}")
def delete_file(file_id: str, ctx: User):
    if not ctx.p.files.delete(ctx.uid, file_id):
        raise ApiError(404, "not_found", "ファイルが見つかりません")
    return {"ok": True}
