"""Accounts, sessions, trusted devices and password management."""
from __future__ import annotations

import re
from typing import Any

from ..audit import AuditLog
from ..config import Settings
from ..db import Database
from ..security.passwords import generate_password, hash_password, password_problems, verify_password
from ..security.tokens import hash_token, new_token
from ..util import dumps, loads, new_id, now

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
ROLES = ("admin", "member")
STATES = ("active", "suspended", "disabled", "deleted")
QUOTA_FIELDS = ("storage_quota_mb", "generation_quota_daily", "concurrent_jobs", "queue_priority", "rate_limit_per_min")
SELF_EDITABLE = ("display_name", "bio", "ui_prefs")
ADMIN_EDITABLE = SELF_EDITABLE + QUOTA_FIELDS + ("role",)


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


def public_user(u: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"],
        "state": u["state"], "must_change_password": bool(u["must_change_password"]), "bio": u["bio"],
        "ui_prefs": loads(u["ui_prefs"], {}), "has_avatar": bool(u["avatar_file"]),
        "storage_quota_mb": u["storage_quota_mb"], "generation_quota_daily": u["generation_quota_daily"],
        "concurrent_jobs": u["concurrent_jobs"], "queue_priority": u["queue_priority"],
        "rate_limit_per_min": u["rate_limit_per_min"], "created_at": u["created_at"],
        "last_login_at": u["last_login_at"], "password_changed_at": u["password_changed_at"],
        "disabled_at": u["disabled_at"], "purge_after": u["purge_after"],
        "is_agent": bool(u.get("is_agent", 0)),
    }


class AuthService:
    def __init__(self, db: Database, settings: Settings, audit: AuditLog):
        self.db, self.settings, self.audit = db, settings, audit

    # ------------------------------------------------------------------ users
    def get_user(self, user_id: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_user_by_name(self, username: str) -> dict | None:
        return self.db.one("SELECT * FROM users WHERE username=? AND state!='deleted'", (username,))

    def list_users(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM users" + ("" if include_deleted else " WHERE state!='deleted'") + " ORDER BY role, username"
        return self.db.query(sql)

    def has_admin(self) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND state='active'"))

    def validate_password(self, password: str, username: str) -> None:
        problems = password_problems(password, username, self.settings.auth.password_min_length)
        if problems:
            raise AuthError("weak_password", " / ".join(problems), 400)

    def create_user(self, *, username: str, password: str | None, role: str = "member",
                    display_name: str | None = None, must_change_password: bool = True,
                    actor: dict | None = None, ip: str | None = None, **fields: Any) -> tuple[dict, str]:
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError("invalid_username", "ユーザー名は3〜32文字の英数字・_ . - で指定してください", 400)
        if role not in ROLES:
            raise AuthError("invalid_role", "ロールが不正です", 400)
        if self.db.one("SELECT id FROM users WHERE username=?", (username,)):
            raise AuthError("username_taken", "そのユーザー名は既に使われています", 409)
        generated = password is None or password == ""
        password = generate_password() if generated else password
        self.validate_password(password, username)
        u = self.settings.users
        values = {
            "storage_quota_mb": self.settings.storage.default_quota_mb,
            "generation_quota_daily": u.default_generation_quota,
            "concurrent_jobs": u.default_concurrent_jobs,
            "queue_priority": 0,
            "rate_limit_per_min": u.default_rate_limit_per_min,
        }
        for k in QUOTA_FIELDS:
            if fields.get(k) is not None:
                values[k] = int(fields[k])
        self._check_quota_values(values)
        uid, ts = new_id(), now()
        self.db.execute(
            "INSERT INTO users(id, username, display_name, password_hash, role, state, must_change_password, bio,"
            " ui_prefs, storage_quota_mb, generation_quota_daily, concurrent_jobs, queue_priority,"
            " rate_limit_per_min, created_at, updated_at, password_changed_at)"
            " VALUES (?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?)",
            (uid, username, (display_name or username).strip()[:64], hash_password(password), role,
             1 if must_change_password else 0, str(fields.get("bio") or "")[:1000], "{}",
             values["storage_quota_mb"], values["generation_quota_daily"], values["concurrent_jobs"],
             values["queue_priority"], values["rate_limit_per_min"], ts, ts, ts),
        )
        self.audit.record("user.create", actor=actor, target=username, ip=ip, role=role)
        return self.get_user(uid), password

    @staticmethod
    def _check_quota_values(values: dict[str, Any]) -> None:
        limits = {"storage_quota_mb": (0, 10_000_000), "generation_quota_daily": (0, 100_000),
                  "concurrent_jobs": (1, 16), "queue_priority": (-2, 2), "rate_limit_per_min": (1, 10_000)}
        for k, (lo, hi) in limits.items():
            if k in values and not lo <= int(values[k]) <= hi:
                raise AuthError("invalid_value", f"{k} は {lo}〜{hi} の範囲で指定してください", 400)

    def update_user(self, user_id: str, changes: dict[str, Any], *, allowed: tuple[str, ...],
                    actor: dict | None, ip: str | None) -> dict:
        user = self.get_user(user_id)
        if not user or user["state"] == "deleted":
            raise AuthError("not_found", "ユーザーが見つかりません", 404)
        forbidden = [k for k in changes if k not in allowed]
        if forbidden:
            raise AuthError("forbidden_field", f"変更できない項目です: {', '.join(forbidden)}", 403)
        values: dict[str, Any] = {}
        for k, v in changes.items():
            if v is None:
                continue
            if k == "display_name":
                v = str(v).strip()[:64]
                if not v:
                    raise AuthError("invalid_value", "表示名を入力してください", 400)
            elif k == "bio":
                v = str(v)[:1000]
            elif k == "ui_prefs":
                if not isinstance(v, dict):
                    raise AuthError("invalid_value", "ui_prefs が不正です", 400)
                v = dumps({str(a)[:40]: b for a, b in list(v.items())[:40] if isinstance(b, (str, int, float, bool))})
            elif k == "role":
                if v not in ROLES:
                    raise AuthError("invalid_role", "ロールが不正です", 400)
                if user["role"] == "admin" and v != "admin" and self._active_admins() <= 1:
                    raise AuthError("last_admin", "最後の管理者のロールは変更できません", 409)
            elif k in QUOTA_FIELDS:
                v = int(v)
            values[k] = v
        self._check_quota_values({k: v for k, v in values.items() if k in QUOTA_FIELDS})
        if values:
            sets = ", ".join(f"{k}=?" for k in values)
            self.db.execute(f"UPDATE users SET {sets}, updated_at=? WHERE id=?", (*values.values(), now(), user_id))
            if "role" in values and values["role"] != user["role"]:
                self.revoke_user_sessions(user_id, "role_changed")
            if actor and actor["id"] != user_id:
                self.audit.record("user.update", actor=actor, target=user["username"], ip=ip, fields=list(values))
        return self.get_user(user_id)

    def _active_admins(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND state='active'") or 0)

    def set_state(self, user_id: str, state: str, *, actor: dict | None, ip: str | None) -> dict:
        if state not in ("active", "suspended", "disabled"):
            raise AuthError("invalid_state", "状態が不正です", 400)
        user = self.get_user(user_id)
        if not user or user["state"] == "deleted":
            raise AuthError("not_found", "ユーザーが見つかりません", 404)
        if actor and actor["id"] == user_id and state != "active":
            raise AuthError("self_lockout", "自分自身を停止することはできません", 409)
        if user["role"] == "admin" and state != "active" and self._active_admins() <= 1:
            raise AuthError("last_admin", "最後の管理者は停止できません", 409)
        ts = now()
        disabled_at = ts if state == "disabled" else None
        purge_after = ts + self.settings.auth.disabled_retention_days * 86400 if state == "disabled" else None
        self.db.execute("UPDATE users SET state=?, disabled_at=?, purge_after=?, updated_at=? WHERE id=?",
                        (state, disabled_at, purge_after, ts, user_id))
        if state != "active":
            self.revoke_user_sessions(user_id, f"account_{state}")
            self.revoke_user_devices(user_id, f"account_{state}")
            self.revoke_user_tokens(user_id, f"account_{state}")
        self.audit.record(f"user.state.{state}", actor=actor, target=user["username"], ip=ip)
        return self.get_user(user_id)

    def mark_deleted(self, user_id: str, *, actor: dict | None, ip: str | None) -> None:
        """Final stage of deletion: row kept as an anonymised tombstone for the audit trail."""
        user = self.get_user(user_id)
        if not user:
            raise AuthError("not_found", "ユーザーが見つかりません", 404)
        ts = now()
        with self.db.tx() as c:
            for table in ("sessions", "devices", "api_tokens", "conversations", "files", "memories", "jobs", "usage_daily"):
                c.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            c.execute("UPDATE users SET state='deleted', username=?, display_name='(deleted)', password_hash='!',"
                      " bio='', ui_prefs='{}', avatar_file=NULL, updated_at=?, purge_after=NULL WHERE id=?",
                      (f"deleted-{user_id[:12]}", ts, user_id))
        self.audit.record("user.delete", actor=actor, target=user["username"], ip=ip)

    # ------------------------------------------------------------------ login throttling
    def _lock_state(self, key: str) -> float:
        row = self.db.one("SELECT locked_until FROM login_failures WHERE key=?", (key,))
        return float(row["locked_until"]) if row else 0.0

    def _record_failure(self, key: str, threshold: int, window: float) -> None:
        a = self.settings.auth
        ts = now()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM login_failures WHERE key=?", (key,)).fetchone()
            if row is None or ts - row["first_at"] > max(window, row["locked_until"] - row["first_at"] + window):
                c.execute("INSERT OR REPLACE INTO login_failures(key, count, first_at, locked_until, lock_level)"
                          " VALUES (?,1,?,0,?)", (key, ts, row["lock_level"] if row else 0))
                return
            count = row["count"] + 1
            locked_until, level = row["locked_until"], row["lock_level"]
            if count >= threshold:
                delay = min(a.lockout_base_seconds * (2 ** level), a.lockout_max_seconds)
                locked_until, level, count = ts + delay, level + 1, 0
            c.execute("UPDATE login_failures SET count=?, locked_until=?, lock_level=? WHERE key=?",
                      (count, locked_until, level, key))

    def authenticate(self, username: str, password: str, ip: str) -> dict:
        a = self.settings.auth
        username = (username or "").strip()[:64]
        keys = [(f"ip:{ip}", a.ip_max_failures, 900.0),
                (f"u:{username.lower()}|{ip}", a.login_max_failures, 900.0),
                (f"u:{username.lower()}", a.login_max_failures * 20, 3600.0)]
        ts = now()
        for key, _, _ in keys:
            until = self._lock_state(key)
            if until > ts:
                raise AuthError("locked", "ログイン試行が多すぎます。しばらく待ってから再試行してください",
                                429, retry_after=until - ts)
        user = self.get_user_by_name(username) if username else None
        ok, rehash = verify_password(user["password_hash"] if user else None, password or "")
        if not ok:
            for key, threshold, window in keys:
                self._record_failure(key, threshold, window)
            self.audit.record("auth.login_failed", target=username, ip=ip)
            raise AuthError("invalid_credentials", "ユーザー名またはパスワードが違います", 401)
        if user["state"] != "active":
            self.audit.record("auth.login_blocked", target=username, ip=ip, state=user["state"])
            raise AuthError("account_inactive", "このアカウントは現在利用できません。管理者に連絡してください", 403)
        self.db.execute("DELETE FROM login_failures WHERE key IN (?, ?)", (keys[1][0], keys[2][0]))
        updates = {"last_login_at": ts}
        if rehash:
            updates["password_hash"] = hash_password(password)
        sets = ", ".join(f"{k}=?" for k in updates)
        self.db.execute(f"UPDATE users SET {sets} WHERE id=?", (*updates.values(), user["id"]))
        return self.get_user(user["id"])

    # ------------------------------------------------------------------ sessions
    def create_session(self, user: dict, *, kind: str, ip: str | None, user_agent: str | None,
                       device_id: str | None = None) -> tuple[str, dict]:
        a = self.settings.auth
        token, sid, ts = new_token(), new_id(), now()
        idle = (a.admin_session_idle_minutes if kind == "admin_app" else a.session_idle_minutes) * 60
        absolute = a.session_absolute_hours * 3600
        self.db.execute(
            "INSERT INTO sessions(id, user_id, token_hash, csrf_token, kind, device_id, created_at, last_seen_at,"
            " idle_expires_at, absolute_expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, user["id"], hash_token(token), new_token(24), kind, device_id, ts, ts, ts + idle, ts + absolute,
             ip, (user_agent or "")[:300]),
        )
        return token, self.db.one("SELECT * FROM sessions WHERE id=?", (sid,))

    def resolve_session(self, token: str) -> tuple[dict, dict] | None:
        if not token or len(token) > 200:
            return None
        s = self.db.one("SELECT * FROM sessions WHERE token_hash=?", (hash_token(token),))
        ts = now()
        if not s or s["revoked_at"] or s["idle_expires_at"] < ts or s["absolute_expires_at"] < ts:
            return None
        user = self.get_user(s["user_id"])
        if not user or user["state"] != "active":
            return None
        if ts - s["last_seen_at"] > 60:
            a = self.settings.auth
            idle = (a.admin_session_idle_minutes if s["kind"] == "admin_app" else a.session_idle_minutes) * 60
            new_idle = min(ts + idle, s["absolute_expires_at"])
            self.db.execute("UPDATE sessions SET last_seen_at=?, idle_expires_at=? WHERE id=?", (ts, new_idle, s["id"]))
            s["last_seen_at"], s["idle_expires_at"] = ts, new_idle
        return user, s

    def list_sessions(self, user_id: str) -> list[dict]:
        ts = now()
        rows = self.db.query(
            "SELECT id, kind, device_id, created_at, last_seen_at, idle_expires_at, absolute_expires_at, ip,"
            " user_agent FROM sessions WHERE user_id=? AND revoked_at IS NULL AND idle_expires_at>? AND"
            " absolute_expires_at>? ORDER BY last_seen_at DESC", (user_id, ts, ts))
        return rows

    def revoke_session(self, session_id: str, reason: str, user_id: str | None = None) -> bool:
        sql = "UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE id=? AND revoked_at IS NULL"
        params: tuple = (now(), reason, session_id)
        if user_id:
            sql += " AND user_id=?"
            params += (user_id,)
        return self.db.execute(sql, params).rowcount > 0

    def revoke_user_sessions(self, user_id: str, reason: str, except_session_id: str | None = None) -> int:
        return self.db.execute(
            "UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE user_id=? AND revoked_at IS NULL AND id!=?",
            (now(), reason, user_id, except_session_id or "")).rowcount

    def revoke_all_sessions(self, reason: str, except_session_id: str | None = None) -> int:
        return self.db.execute("UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE revoked_at IS NULL AND id!=?",
                               (now(), reason, except_session_id or "")).rowcount

    # ------------------------------------------------------------------ trusted devices
    def register_device(self, user: dict, *, name: str | None, ip: str | None, user_agent: str | None) -> tuple[str, dict]:
        a = self.settings.auth
        token, did, ts = new_token(), new_id(), now()
        name = (name or "").strip()[:60] or _device_name_from_ua(user_agent)
        self.db.execute(
            "INSERT INTO devices(id, user_id, name, token_hash, created_at, last_used_at, expires_at, max_expires_at,"
            " last_ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, user["id"], name, hash_token(token), ts, ts, ts + a.device_days * 86400,
             ts + a.device_max_days * 86400, ip, (user_agent or "")[:300]))
        self.audit.record("device.register", actor=user, target=did, ip=ip, name=name)
        return token, self.db.one("SELECT * FROM devices WHERE id=?", (did,))

    def refresh_with_device(self, token: str, *, ip: str | None, user_agent: str | None) -> tuple[dict, dict, str | None]:
        """Validate a trusted-device token and rotate it. Returns (user, device, new_token_or_None)."""
        if not token or len(token) > 200:
            raise AuthError("device_invalid", "端末の認証情報が無効です")
        a = self.settings.auth
        h, ts = hash_token(token), now()
        dev = self.db.one("SELECT * FROM devices WHERE token_hash=?", (h,))
        if dev is None:
            old = self.db.one("SELECT * FROM devices WHERE prev_token_hash=?", (h,))
            if old and not old["revoked_at"] and old["rotated_at"] and ts - old["rotated_at"] <= a.device_rotation_grace_seconds:
                user = self._active_user_or_raise(old["user_id"])
                return user, old, None
            if old and not old["revoked_at"]:
                self.revoke_device(old["id"], "token_reuse_detected")
                user = self.get_user(old["user_id"])
                self.audit.record("device.token_reuse", actor=user, target=old["id"], ip=ip)
            raise AuthError("device_invalid", "端末の認証情報が無効です。再度ログインしてください")
        if dev["revoked_at"] or dev["expires_at"] < ts or dev["max_expires_at"] < ts:
            raise AuthError("device_expired", "端末の信頼期限が切れました。再度ログインしてください")
        user = self._active_user_or_raise(dev["user_id"])
        new = new_token()
        new_exp = min(ts + a.device_days * 86400, dev["max_expires_at"])
        self.db.execute(
            "UPDATE devices SET prev_token_hash=token_hash, token_hash=?, rotated_at=?, last_used_at=?, expires_at=?,"
            " last_ip=?, user_agent=? WHERE id=?",
            (hash_token(new), ts, ts, new_exp, ip, (user_agent or "")[:300], dev["id"]))
        return user, self.db.one("SELECT * FROM devices WHERE id=?", (dev["id"],)), new

    def _active_user_or_raise(self, user_id: str) -> dict:
        user = self.get_user(user_id)
        if not user or user["state"] != "active":
            raise AuthError("account_inactive", "このアカウントは現在利用できません", 403)
        return user

    def list_devices(self, user_id: str) -> list[dict]:
        ts = now()
        rows = self.db.query(
            "SELECT id, name, created_at, last_used_at, expires_at, last_ip, user_agent, revoked_at, revoke_reason"
            " FROM devices WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        for r in rows:
            r["status"] = "revoked" if r["revoked_at"] else "expired" if r["expires_at"] < ts else "active"
            r["active_sessions"] = int(self.db.scalar(
                "SELECT COUNT(*) FROM sessions WHERE device_id=? AND revoked_at IS NULL AND idle_expires_at>?",
                (r["id"], ts)) or 0)
        return rows

    def rename_device(self, device_id: str, user_id: str, name: str) -> bool:
        name = (name or "").strip()[:60]
        if not name:
            raise AuthError("invalid_value", "端末名を入力してください", 400)
        return self.db.execute("UPDATE devices SET name=? WHERE id=? AND user_id=?", (name, device_id, user_id)).rowcount > 0

    def revoke_device(self, device_id: str, reason: str, user_id: str | None = None) -> bool:
        sql = "UPDATE devices SET revoked_at=?, revoke_reason=? WHERE id=? AND revoked_at IS NULL"
        params: tuple = (now(), reason, device_id)
        if user_id:
            sql += " AND user_id=?"
            params += (user_id,)
        changed = self.db.execute(sql, params).rowcount > 0
        if changed:
            self.db.execute("UPDATE sessions SET revoked_at=?, revoke_reason=? WHERE device_id=? AND revoked_at IS NULL",
                            (now(), "device_revoked", device_id))
        return changed

    def revoke_user_devices(self, user_id: str, reason: str) -> int:
        ids = [r["id"] for r in self.db.query("SELECT id FROM devices WHERE user_id=? AND revoked_at IS NULL", (user_id,))]
        for did in ids:
            self.revoke_device(did, reason)
        return len(ids)

    # ------------------------------------------------------------------ passwords
    def change_password(self, user_id: str, current: str, new: str, *, revoke_others: bool,
                        current_session_id: str | None, ip: str | None) -> None:
        user = self._active_user_or_raise(user_id)
        ok, _ = verify_password(user["password_hash"], current or "")
        if not ok:
            self.audit.record("auth.password_change_failed", actor=user, target=user["username"], ip=ip)
            raise AuthError("invalid_credentials", "現在のパスワードが違います", 400)
        if current == new:
            raise AuthError("same_password", "新しいパスワードは現在と異なるものにしてください", 400)
        self.validate_password(new, user["username"])
        self.db.execute("UPDATE users SET password_hash=?, must_change_password=0, password_changed_at=?, updated_at=?"
                        " WHERE id=?", (hash_password(new), now(), now(), user_id))
        if revoke_others:
            self.revoke_user_sessions(user_id, "password_changed", except_session_id=current_session_id)
            current_device = None
            if current_session_id:
                s = self.db.one("SELECT device_id FROM sessions WHERE id=?", (current_session_id,))
                current_device = s["device_id"] if s else None
            for d in self.db.query("SELECT id FROM devices WHERE user_id=? AND revoked_at IS NULL", (user_id,)):
                if d["id"] != current_device:
                    self.revoke_device(d["id"], "password_changed")
        self.audit.record("auth.password_changed", actor=user, target=user["username"], ip=ip, revoke_others=revoke_others)

    def reset_password(self, user_id: str, new_password: str | None, *, must_change: bool,
                       actor: dict | None, ip: str | None) -> str:
        user = self.get_user(user_id)
        if not user or user["state"] == "deleted":
            raise AuthError("not_found", "ユーザーが見つかりません", 404)
        pw = new_password or generate_password()
        self.validate_password(pw, user["username"])
        self.db.execute("UPDATE users SET password_hash=?, must_change_password=?, password_changed_at=?, updated_at=?"
                        " WHERE id=?", (hash_password(pw), 1 if must_change else 0, now(), now(), user_id))
        self.db.execute("DELETE FROM login_failures WHERE key LIKE ?", (f"u:{user['username'].lower()}%",))
        self.revoke_user_sessions(user_id, "password_reset")
        self.revoke_user_devices(user_id, "password_reset")
        self.revoke_user_tokens(user_id, "password_reset")
        self.audit.record("user.password_reset", actor=actor, target=user["username"], ip=ip)
        return pw

    # ------------------------------------------------------------------ API tokens (automation / Claude)
    def issue_api_token(self, user: dict, *, scopes: list[str], days: float, name: str,
                        actor: dict | None, ip: str | None) -> tuple[str, dict]:
        bad = [x for x in scopes if x not in TOKEN_SCOPES]
        if bad or not scopes:
            raise AuthError("invalid_scope", f"スコープが不正です: {bad or scopes}", 400)
        if not 0 < days <= 90:
            raise AuthError("invalid_value", "有効期限は1〜90日で指定してください", 400)
        token, tid, ts = API_TOKEN_PREFIX + new_token(32), new_id(), now()
        self.db.execute(
            "INSERT INTO api_tokens(id, user_id, name, token_hash, scopes, created_at, created_by, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (tid, user["id"], name[:60], hash_token(token), ",".join(scopes), ts,
             actor["username"] if actor else "cli", ts + days * 86400))
        self.audit.record("token.issue", actor=actor, target=user["username"], ip=ip, token_id=tid, scopes=scopes,
                          days=days)
        return token, self.db.one("SELECT * FROM api_tokens WHERE id=?", (tid,))

    def resolve_api_token(self, token: str, ip: str | None = None) -> tuple[dict, dict] | None:
        if not token.startswith(API_TOKEN_PREFIX) or len(token) > 200:
            return None
        row = self.db.one("SELECT * FROM api_tokens WHERE token_hash=?", (hash_token(token),))
        ts = now()
        if not row or row["revoked_at"] or row["expires_at"] < ts:
            return None
        user = self.get_user(row["user_id"])
        if not user or user["state"] != "active":
            return None
        if not row["last_used_at"] or ts - row["last_used_at"] > 60:
            self.db.execute("UPDATE api_tokens SET last_used_at=?, last_ip=? WHERE id=?", (ts, ip, row["id"]))
        row["scopes"] = row["scopes"].split(",")
        return user, row

    def list_api_tokens(self) -> list[dict]:
        ts = now()
        rows = self.db.query("SELECT t.id, t.name, t.scopes, t.created_at, t.created_by, t.expires_at, t.last_used_at,"
                             " t.last_ip, t.revoked_at, t.revoke_reason, u.username FROM api_tokens t"
                             " JOIN users u ON u.id=t.user_id ORDER BY t.created_at DESC")
        for r in rows:
            r["scopes"] = r["scopes"].split(",")
            r["status"] = "revoked" if r["revoked_at"] else "expired" if r["expires_at"] < ts else "active"
        return rows

    def revoke_api_token(self, token_id: str, reason: str, *, actor: dict | None = None, ip: str | None = None) -> bool:
        ok = self.db.execute("UPDATE api_tokens SET revoked_at=?, revoke_reason=? WHERE id=? AND revoked_at IS NULL",
                             (now(), reason, token_id)).rowcount > 0
        if ok:
            self.audit.record("token.revoke", actor=actor, target=token_id, ip=ip, reason=reason)
        return ok

    def revoke_user_tokens(self, user_id: str, reason: str) -> int:
        return self.db.execute("UPDATE api_tokens SET revoked_at=?, revoke_reason=? WHERE user_id=? AND revoked_at IS NULL",
                               (now(), reason, user_id)).rowcount

    def ensure_agent_account(self, *, days: float, debug: bool, actor: dict | None, ip: str | None) -> dict:
        """Create (or rotate) the dedicated Claude account: a member for UI work + a scoped, expiring API token.
        Re-issuing always rotates the password and revokes every previous token of the account."""
        user = self.get_user_by_name(AGENT_USERNAME)
        if user is None:
            user, password = self.create_user(username=AGENT_USERNAME, password=None, role="member",
                                              display_name="Claude (AIデバッグ)", must_change_password=False,
                                              actor=actor, ip=ip, bio="Claude Code によるデバッグ・UI検証用の専用アカウント")
        else:
            if user["role"] != "member":
                raise AuthError("agent_conflict", "ユーザー名 claude が管理者として既に使われています", 409)
            if user["state"] != "active":
                self.set_state(user["id"], "active", actor=actor, ip=ip)
            password = self.reset_password(user["id"], None, must_change=False, actor=actor, ip=ip)
        self.db.execute("UPDATE users SET is_agent=1 WHERE id=?", (user["id"],))
        self.revoke_user_tokens(user["id"], "rotated")
        scopes = ["member", "debug"] if debug else ["member"]
        token, row = self.issue_api_token(self.get_user(user["id"]), scopes=scopes, days=days, name="Claude Code",
                                          actor=actor, ip=ip)
        return {"username": AGENT_USERNAME, "password": password, "token": token, "token_id": row["id"],
                "scopes": scopes, "expires_at": row["expires_at"]}

    def cleanup(self) -> dict[str, int]:
        ts = now()
        s = self.db.execute("DELETE FROM sessions WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR"
                            " absolute_expires_at < ?", (ts - 30 * 86400, ts - 86400)).rowcount
        d = self.db.execute("DELETE FROM devices WHERE (revoked_at IS NOT NULL AND revoked_at < ?) OR"
                            " max_expires_at < ?", (ts - 90 * 86400, ts - 86400)).rowcount
        f = self.db.execute("DELETE FROM login_failures WHERE locked_until < ? AND first_at < ?",
                            (ts, ts - 86400)).rowcount
        return {"sessions": s, "devices": d, "login_failures": f}

    def users_due_for_purge(self) -> list[dict]:
        return self.db.query("SELECT * FROM users WHERE state='disabled' AND purge_after IS NOT NULL AND purge_after < ?",
                             (now(),))


API_TOKEN_PREFIX = "nxt_"
TOKEN_SCOPES = ("member", "debug")
AGENT_USERNAME = "claude"


def _device_name_from_ua(ua: str | None) -> str:
    ua = ua or ""
    os_name = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                   ("Windows", "Windows"), ("Mac OS X", "Mac"), ("Linux", "Linux")) if k in ua), "端末")
    browser = next((n for k, n in (("Edg/", "Edge"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"),
                                   ("Safari/", "Safari")) if k in ua), "ブラウザ")
    return f"{os_name} / {browser}"
