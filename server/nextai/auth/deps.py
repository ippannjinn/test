"""Request authentication, CSRF, RBAC and per-user API rate limiting."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from ..security.tokens import safe_equal

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Admin-API calls a "debug"-scoped automation token may make: every read, plus running diagnostics.
DEBUG_TOKEN_POSTS = {"/api/admin/diagnostics/run"}
PASSWORD_GATE_ALLOWED = {"/api/auth/session", "/api/auth/password", "/api/auth/logout", "/api/account/profile"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers


@dataclass
class Ctx:
    p: Any
    user: dict
    session: dict
    auth: str
    ip: str
    scopes: tuple = ()

    @property
    def uid(self) -> str:
        return self.user["id"]


def cookie_names(settings) -> tuple[str, str]:
    if settings.server.cookie_secure:
        return "__Host-nextai_sid", "__Host-nextai_dev"
    return "nextai_sid", "nextai_dev"


def via_tunnel(request: Request) -> bool:
    return bool(request.scope.get("state", {}).get("via_tunnel"))


def client_ip(request: Request) -> str:
    p = request.app.state.platform
    host = request.client.host if request.client else "0.0.0.0"
    if via_tunnel(request):
        # The tunnel (tailscaled) appends the real client address; earlier entries are client-controlled.
        xff = request.headers.get("x-forwarded-for", "")
        last = xff.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            return "tunnel"
    if host in p.settings.server.trusted_proxies:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()
    return host


_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "true-client-ip", "x-forwarded-host")


def is_local_admin_request(request: Request, ip: str) -> bool:
    """Loopback peer AND no proxy headers: a tunnel/reverse proxy on this PC must not make remote
    traffic look local to the admin API."""
    if via_tunnel(request) or any(h in request.headers for h in _PROXY_HEADERS):
        return False
    return is_loopback(ip)


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return ip in ("testclient",)


def _resolve(request: Request) -> Ctx | None:
    p = request.app.state.platform
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token, kind = authz[7:].strip(), "bearer"
    else:
        token, kind = request.cookies.get(cookie_names(p.settings)[0], ""), "cookie"
    if not token:
        return None
    if kind == "bearer" and token.startswith("nxt_"):
        ip = client_ip(request)
        res = p.auth.resolve_api_token(token, ip)
        if not res:
            return None
        user, tok = res
        sess = {"id": f"token:{tok['id']}", "kind": "api_token", "csrf_token": "", "device_id": None, "user_agent": ""}
        return Ctx(p, user, sess, "token", ip, tuple(tok["scopes"]))
    res = p.auth.resolve_session(token)
    if not res:
        return None
    user, sess = res
    if (kind == "bearer") != (sess["kind"] == "admin_app"):
        return None
    return Ctx(p, user, sess, kind, client_ip(request))


def allowed_origin_hosts(request: Request) -> set[str]:
    hosts = {request.headers.get("host", "")}
    public = request.app.state.platform.settings.server.public_url
    if public:
        hosts.add(public.split("://", 1)[-1].split("/", 1)[0])
    if via_tunnel(request) and request.headers.get("x-forwarded-host"):
        hosts.add(request.headers["x-forwarded-host"])
    return hosts


def require_user(request: Request, _admin: bool = False) -> Ctx:
    ctx = _resolve(request)
    if ctx is None:
        raise ApiError(401, "unauthenticated", "ログインが必要です")
    if ctx.auth == "token" and not _admin and "member" not in ctx.scopes:
        raise ApiError(403, "token_scope", "このトークンには一般APIの権限がありません")
    if ctx.auth == "cookie" and request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not safe_equal(sent, ctx.session["csrf_token"]):
            raise ApiError(403, "csrf", "CSRFトークンが無効です。ページを再読み込みしてください")
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] not in allowed_origin_hosts(request):
            raise ApiError(403, "bad_origin", "不正なオリジンからのリクエストです")
    if ctx.user["must_change_password"] and request.url.path not in PASSWORD_GATE_ALLOWED:
        raise ApiError(403, "password_change_required", "初回ログインのためパスワードの変更が必要です")
    a = ctx.p.settings.auth
    ok, retry = ctx.p.ratelimiter.hit(f"api:{ctx.uid}", a.api_rate_per_second, a.api_burst)
    if not ok:
        raise ApiError(429, "rate_limited", "リクエストが多すぎます", {"Retry-After": str(int(retry) + 1)})
    return ctx


def require_admin(request: Request) -> Ctx:
    ctx = require_user(request, _admin=True)
    if ctx.auth == "token":
        if "debug" not in ctx.scopes:
            raise ApiError(403, "token_scope", "このトークンには管理APIの権限がありません")
        if request.method not in SAFE_METHODS and request.url.path not in DEBUG_TOKEN_POSTS:
            raise ApiError(403, "debug_read_only", "デバッグ用トークンは読み取りと診断の実行のみ可能です")
        if not ctx.p.settings.server.allow_remote_admin and not is_local_admin_request(request, ctx.ip):
            raise ApiError(403, "admin_local_only", "管理APIはサーバーPC上からのみ利用できます")
        return ctx
    if ctx.user["role"] != "admin":
        raise ApiError(403, "forbidden", "管理者権限が必要です")
    if ctx.auth != "bearer":
        raise ApiError(403, "admin_app_only", "管理機能は管理デスクトップアプリからのみ利用できます")
    if not ctx.p.settings.server.allow_remote_admin and not is_local_admin_request(request, ctx.ip):
        raise ApiError(403, "admin_local_only", "管理APIはサーバーPC上からのみ利用できます")
    return ctx
