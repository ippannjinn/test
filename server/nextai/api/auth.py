from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, client_ip, cookie_names, is_loopback, require_user
from ..auth.service import AuthError, public_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    trust_device: bool = False
    device_name: str | None = Field(default=None, max_length=60)
    client: Literal["web", "admin_app"] = "web"


class LogoutBody(BaseModel):
    forget_device: bool = False


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)
    revoke_other_sessions: bool = True


def _require_xhr(request: Request) -> None:
    if request.headers.get("x-requested-with") != "nextai":
        raise ApiError(403, "csrf", "不正なリクエストです")


def _set_cookies(p, response: Response, *, session_token: str | None = None, device_token: str | None = None,
                 persistent: bool = False) -> None:
    sid, dev = cookie_names(p.settings)
    secure = p.settings.server.cookie_secure
    if session_token is not None:
        response.set_cookie(sid, session_token, httponly=True, secure=secure, samesite="lax", path="/",
                            max_age=p.settings.auth.session_absolute_hours * 3600 if persistent else None)
    if device_token is not None:
        response.set_cookie(dev, device_token, httponly=True, secure=secure, samesite="strict", path="/",
                            max_age=p.settings.auth.device_days * 86400)


def _session_payload(p, user: dict, sess: dict) -> dict:
    return {"user": public_user(user), "csrf_token": sess["csrf_token"],
            "must_change_password": bool(user["must_change_password"]), "trusted_device": bool(sess.get("device_id")),
            "server": {"name": p.settings.server.name, "version": p.version}}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    p = request.app.state.platform
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    ok, retry = p.ratelimiter.hit(f"login:{ip}", 0.5, 10)
    if not ok:
        raise ApiError(429, "rate_limited", "ログイン試行が多すぎます", {"Retry-After": str(int(retry) + 1)})
    if body.client == "admin_app":
        if not p.settings.server.allow_remote_admin and not is_loopback(ip):
            raise ApiError(403, "admin_local_only", "管理アプリはサーバーPC上からのみログインできます")
    else:
        _require_xhr(request)
    try:
        user = p.auth.authenticate(body.username, body.password, ip)
    except AuthError as e:
        raise ApiError(e.status, e.code, e.message, {"Retry-After": str(int(e.retry_after) + 1)} if e.retry_after else None)
    if body.client == "admin_app":
        if user["role"] != "admin":
            p.audit.record("auth.admin_login_denied", actor=user, ip=ip)
            raise ApiError(403, "forbidden", "管理者アカウントでログインしてください")
        token, sess = p.auth.create_session(user, kind="admin_app", ip=ip, user_agent=ua)
        p.audit.record("auth.admin_login", actor=user, ip=ip)
        return {"token": token, "user": public_user(user), "idle_expires_at": sess["idle_expires_at"]}
    device_id = None
    if body.trust_device:
        dtoken, dev = p.auth.register_device(user, name=body.device_name, ip=ip, user_agent=ua)
        device_id = dev["id"]
        _set_cookies(p, response, device_token=dtoken)
    token, sess = p.auth.create_session(user, kind="web", ip=ip, user_agent=ua, device_id=device_id)
    _set_cookies(p, response, session_token=token, persistent=body.trust_device)
    p.audit.record("auth.login", actor=user, ip=ip, trusted_device=body.trust_device)
    return _session_payload(p, user, sess)


@router.post("/refresh")
def refresh(request: Request, response: Response):
    p = request.app.state.platform
    _require_xhr(request)
    ip, ua = client_ip(request), request.headers.get("user-agent", "")
    ok, retry = p.ratelimiter.hit(f"refresh:{ip}", 0.5, 10)
    if not ok:
        raise ApiError(429, "rate_limited", "リクエストが多すぎます")
    dtoken = request.cookies.get(cookie_names(p.settings)[1], "")
    if not dtoken:
        raise ApiError(401, "no_device", "信頼済み端末ではありません")
    try:
        user, dev, new_token = p.auth.refresh_with_device(dtoken, ip=ip, user_agent=ua)
    except AuthError as e:
        response.delete_cookie(cookie_names(p.settings)[1], path="/")
        raise ApiError(e.status, e.code, e.message)
    if new_token:
        _set_cookies(p, response, device_token=new_token)
    token, sess = p.auth.create_session(user, kind="web", ip=ip, user_agent=ua, device_id=dev["id"])
    _set_cookies(p, response, session_token=token, persistent=True)
    return _session_payload(p, user, sess)


@router.get("/session")
def session(ctx: Annotated[Ctx, Depends(require_user)]):
    return _session_payload(ctx.p, ctx.user, ctx.session)


@router.post("/logout")
def logout(body: LogoutBody, ctx: Annotated[Ctx, Depends(require_user)], response: Response):
    p = ctx.p
    p.auth.revoke_session(ctx.session["id"], "logout")
    sid, dev = cookie_names(p.settings)
    response.delete_cookie(sid, path="/")
    if body.forget_device and ctx.session.get("device_id"):
        p.auth.revoke_device(ctx.session["device_id"], "user_logout", user_id=ctx.uid)
        response.delete_cookie(dev, path="/")
    p.audit.record("auth.logout", actor=ctx.user, ip=ctx.ip, forget_device=body.forget_device)
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordBody, ctx: Annotated[Ctx, Depends(require_user)], response: Response):
    p = ctx.p
    try:
        p.auth.change_password(ctx.uid, body.current_password, body.new_password,
                               revoke_others=body.revoke_other_sessions, current_session_id=ctx.session["id"], ip=ctx.ip)
    except AuthError as e:
        raise ApiError(e.status, e.code, e.message)
    user = p.auth.get_user(ctx.uid)
    token, sess = p.auth.create_session(user, kind=ctx.session["kind"], ip=ctx.ip,
                                        user_agent=ctx.session.get("user_agent"), device_id=ctx.session.get("device_id"))
    p.auth.revoke_session(ctx.session["id"], "rotated_after_password_change")
    if ctx.auth == "cookie":
        _set_cookies(p, response, session_token=token, persistent=bool(ctx.session.get("device_id")))
        return _session_payload(p, user, sess)
    return {"token": token, "user": public_user(user)}
