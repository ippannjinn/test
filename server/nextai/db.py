from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from .util import now


class Database:
    """SQLite with WAL, one connection per thread, explicit write transactions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        except sqlite3.OperationalError as e:
            d = self.path.parent
            raise sqlite3.OperationalError(
                f"{e}: {self.path} (exists={self.path.exists()}, dir_exists={d.exists()}, "
                f"readable={os.access(self.path, os.R_OK)}, writable={os.access(self.path, os.W_OK)}, "
                f"dir_writable={os.access(d, os.W_OK)})") from e
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        with self._all_lock:
            self._all.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        r = self.conn.execute(sql, params).fetchone()
        return r[0] if r is not None else None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        with self._lock:
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def migrate(self) -> list[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.conn
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        applied = []
        files = sorted(
            (f for f in resources.files("nextai.migrations").iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        for f in files:
            if f.name in done:
                continue
            sql = f.read_text(encoding="utf-8")
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split_sql(sql):
                        conn.execute(stmt)
                    conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)", (f.name, now()))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            applied.append(f.name)
        return applied

    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(self.path))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

    def close(self) -> None:
        with self._all_lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()


def _split_sql(sql: str) -> list[str]:
    stmts, buf = [], []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            s = "\n".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts
