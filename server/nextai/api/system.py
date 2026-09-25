from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from ..auth.deps import ApiError, Ctx, require_user
from ..models.manager import HOT

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health(request: Request):
    p = request.app.state.platform
    return {"status": "ok", "version": p.version}


@router.get("/api/info")
def info(request: Request):
    p = request.app.state.platform
    return {"name": p.settings.server.name, "version": p.version, "login_message": p.settings.server.login_message,
            "https": p.settings.server.tls}


@router.get("/ca.crt")
def ca_cert(request: Request):
    p = request.app.state.platform
    path = p.settings.paths.certs / "ca.crt"
    if not path.exists():
        return JSONResponse({"error": {"code": "not_found", "message": "CA証明書がありません"}}, 404)
    return FileResponse(path, media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="nextai-ca.crt"'})


@router.get("/api/status")
def status(ctx: Annotated[Ctx, Depends(require_user)]):
    p = ctx.p
    st = p.scheduler.stats()
    hot = [{"id": m, "name": rt.spec.display_name, "kind": rt.spec.kind}
           for m, rt in p.models.runtimes.items() if rt.state == HOT]
    return {"queue": {"pending": st["pending"], "running": st["running"], "est_wait_seconds": st["est_wait_seconds"],
                      "congestion": st["congestion"]},
            "load_level": p.governor.state.level.name, "models_loaded": hot,
            "capabilities": {k: bool(p.models.usable_models(v)) for k, v in
                             {"chat": ("llm",), "vision": ("vlm",), "image": ("image",), "video": ("video",),
                              "music": ("music",)}.items()} | {"code": p.sandbox.available, "web": True}}


def not_found(*_):
    raise ApiError(404, "not_found", "Not found")
