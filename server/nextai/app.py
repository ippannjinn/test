from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import account, admin, auth, chat, files, generate, jobs, memory, openai, system
from .auth.deps import ApiError
from .auth.service import AuthError
from .jobs import AdmissionError
from .models.manager import ModelUnavailable
from .platform import Platform
from .scheduler import QueueTimeout
from .services.files import FileTooLarge, QuotaExceeded
from .services.storage import DiskFull

log = logging.getLogger("nextai.app")

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'; manifest-src 'self'")
UPLOAD_PATHS = ("/api/files", "/api/account/avatar")
LARGE_JSON_PATHS = ("/v1/chat/completions",)  # may carry base64 images


class SecurityMiddleware:
    """Pure ASGI (keeps SSE streaming intact): security headers + request body size limits."""

    def __init__(self, app, platform: Platform):
        self.app, self.p = app, platform

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        s = self.p.settings.server
        server = scope.get("server") or ("", 0)
        if s.tunnel_port and server[1] == s.tunnel_port:
            # Arrived on the loopback-only listener that Tailscale Funnel (or another tunnel) forwards
            # internet traffic to: never treated as local, client IP taken from the tunnel's header.
            scope.setdefault("state", {})["via_tunnel"] = True
        limit = s.max_upload_mb * 2**20 + 65536 if path in UPLOAD_PATHS else s.max_json_kb * 1024
        if path in LARGE_JSON_PATHS:
            limit = max(limit, 24 * 2**20)
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None and cl.isdigit() and int(cl) > limit:
            return await _send_json(send, 413, "too_large", "リクエストが大きすぎます")
        if scope["method"] in ("POST", "PUT", "PATCH") and path in UPLOAD_PATHS and cl is None:
            return await _send_json(send, 411, "length_required", "Content-Length が必要です")
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return msg

        is_api = path.startswith(("/api/", "/v1/"))
        tls = s.tls

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                h = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in h}
                add = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                       (b"referrer-policy", b"no-referrer"), (b"cross-origin-opener-policy", b"same-origin"),
                       (b"permissions-policy", b"camera=(), microphone=(self), geolocation=()")]
                if b"content-security-policy" not in existing:
                    add.append((b"content-security-policy", CSP.encode()))
                if tls:
                    add.append((b"strict-transport-security", b"max-age=31536000"))
                if is_api and b"cache-control" not in existing:
                    add.append((b"cache-control", b"no-store"))
                elif not is_api and b"cache-control" not in existing:
                    # web UI files: always revalidate (ETag) so an update is picked up without a hard reload
                    add.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": h + [x for x in add if x[0] not in existing]}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except _TooLarge:
            if not started:
                await _send_json(send, 413, "too_large", "リクエストが大きすぎます")


class _TooLarge(Exception):
    pass


async def _send_json(send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def create_app(platform: Platform, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_lifecycle:
            await platform.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await platform.stop()

    app = FastAPI(title="NextAI Platform", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.platform = platform

    @app.exception_handler(ApiError)
    async def api_error(_: Request, e: ApiError):
        return _err(e.status, e.code, e.message, e.headers)

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, e: AuthError):
        return _err(e.status, e.code, e.message)

    @app.exception_handler(AdmissionError)
    async def admission_error(_: Request, e: AdmissionError):
        return _err(e.status, e.code, e.message)

    @app.exception_handler(QuotaExceeded)
    async def quota_error(_: Request, e: QuotaExceeded):
        return _err(507, "quota_exceeded", str(e))

    @app.exception_handler(FileTooLarge)
    async def too_large(_: Request, e: FileTooLarge):
        return _err(413, "too_large", str(e))

    @app.exception_handler(DiskFull)
    async def disk_full(_: Request, e: DiskFull):
        return _err(507, "disk_full", str(e))

    @app.exception_handler(ModelUnavailable)
    async def model_unavailable(_: Request, e: ModelUnavailable):
        return _err(503, "model_unavailable", str(e))

    @app.exception_handler(QueueTimeout)
    async def queue_timeout(_: Request, e: QueueTimeout):
        return _err(503, "queue_timeout", str(e))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return _err(422, "invalid_request", f"入力が不正です: {loc} {first.get('msg', '')}".strip())

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, e: StarletteHTTPException):
        return _err(e.status_code, f"http_{e.status_code}", str(e.detail))

    @app.exception_handler(Exception)
    async def unhandled(_: Request, e: Exception):
        log.exception("unhandled error: %s", e)
        return _err(500, "internal_error", "サーバー内部エラーが発生しました")

    for r in (system.router, auth.router, account.router, chat.router, jobs.router, files.router, generate.router,
              memory.router, admin.router, openai.router, files.preview_router):
        app.include_router(r)

    web_dir = Path(str(resources.files("nextai").joinpath("web")))
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    app.add_middleware(SecurityMiddleware, platform=platform)
    return app
