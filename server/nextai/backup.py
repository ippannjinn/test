"""Backup (DB + config + certs + user data) and restore-on-restart."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import Settings
from .db import Database

log = logging.getLogger("nextai.backup")
PENDING = "pending_restore.json"


def create_backup(settings: Settings, db: Database, include_user_files: bool = True) -> Path:
    p = settings.paths
    p.backups.mkdir(parents=True, exist_ok=True)
    out = p.backups / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with tempfile.TemporaryDirectory(dir=p.tmp) as td:
        snap = Path(td) / "nextai.db"
        db.backup_to(snap)
        tmp_out = out.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.writestr("manifest.json", json.dumps({"version": __version__, "created_at": time.time(),
                                                    "include_user_files": include_user_files}))
            z.write(snap, "nextai.db")
            if p.config_file.exists():
                z.write(p.config_file, "config.toml")
            for f in sorted(p.certs.glob("*")) if p.certs.exists() else []:
                if f.is_file():
                    z.write(f, f"certs/{f.name}")
            if include_user_files and p.users.exists():
                for f in p.users.rglob("*"):
                    rel = f.relative_to(p.users).as_posix()
                    if f.is_file() and "/sandbox/" not in f"/{rel}" and not f.name.startswith(".upload-"):
                        z.write(f, f"users/{rel}")
        os.replace(tmp_out, out)
    log.info("backup created %s (%.1f MB)", out.name, out.stat().st_size / 2**20)
    return out


def list_backups(settings: Settings) -> list[dict]:
    p = settings.paths.backups
    if not p.exists():
        return []
    return [{"name": f.name, "size": f.stat().st_size, "created_at": f.stat().st_mtime}
            for f in sorted(p.glob("backup-*.zip"), reverse=True)]


def _validate(zpath: Path) -> list[str]:
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        if "nextai.db" not in names or "manifest.json" not in names:
            raise ValueError("バックアップファイルの形式が不正です")
        for n in names:
            if n.startswith(("/", "\\")) or ".." in Path(n).parts or ":" in n:
                raise ValueError(f"不正なパスを含んでいます: {n}")
            if not (n in ("manifest.json", "nextai.db", "config.toml") or n.startswith(("certs/", "users/"))):
                raise ValueError(f"想定外のファイルを含んでいます: {n}")
        return names


def schedule_restore(settings: Settings, name: str) -> None:
    src = settings.paths.backups / Path(name).name
    if not src.exists():
        raise FileNotFoundError(name)
    _validate(src)
    (settings.paths.data_dir / PENDING).write_text(json.dumps({"backup": src.name, "at": time.time()}), encoding="utf-8")


def apply_pending_restore(settings: Settings) -> bool:
    marker = settings.paths.data_dir / PENDING
    if not marker.exists():
        return False
    info = json.loads(marker.read_text(encoding="utf-8"))
    marker.unlink()
    return restore_now(settings, settings.paths.backups / Path(info["backup"]).name)


def restore_now(settings: Settings, zpath: Path) -> bool:
    """Must run while the server is stopped. The previous state is kept in restore-rollback-*/."""
    p = settings.paths
    names = _validate(zpath)
    rollback = p.data_dir / f"restore-rollback-{time.strftime('%Y%m%d-%H%M%S')}"
    rollback.mkdir(parents=True)
    for item in ("nextai.db", "nextai.db-wal", "nextai.db-shm", "config.toml"):
        src = p.data_dir / item
        if src.exists():
            shutil.move(str(src), rollback / item)
    has_users = any(n.startswith("users/") for n in names)
    if has_users and p.users.exists():
        shutil.move(str(p.users), rollback / "users")
    with zipfile.ZipFile(zpath) as z:
        for n in names:
            if n == "manifest.json" or n.endswith("/"):
                continue
            dest = p.data_dir / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    log.warning("restored backup %s (previous state in %s)", zpath.name, rollback.name)
    return True
