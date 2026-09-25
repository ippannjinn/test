from __future__ import annotations

import logging
from typing import Any

from .db import Database
from .util import dumps, loads, now

log = logging.getLogger("nextai.audit")


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def record(self, action: str, *, actor: dict | None = None, target: str | None = None,
               ip: str | None = None, **details: Any) -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts, actor_id, actor_name, action, target, ip, details) VALUES (?,?,?,?,?,?,?)",
            (now(), actor["id"] if actor else None, actor["username"] if actor else None, action, target, ip,
             dumps(details) if details else None),
        )
        log.info("audit %s actor=%s target=%s ip=%s", action, actor["username"] if actor else "-", target, ip)

    def query(self, *, limit: int = 200, offset: int = 0, action: str | None = None,
              actor: str | None = None, since: float | None = None) -> list[dict]:
        where, params = [], []
        if action:
            where.append("action LIKE ?")
            params.append(action.replace("*", "%"))
        if actor:
            where.append("actor_name = ?")
            params.append(actor)
        if since:
            where.append("ts >= ?")
            params.append(since)
        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [min(max(limit, 1), 2000), max(offset, 0)]
        rows = self.db.query(sql, tuple(params))
        for r in rows:
            r["details"] = loads(r["details"], {})
        return rows
