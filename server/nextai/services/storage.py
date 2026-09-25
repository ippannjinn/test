"""Disk safety: keep the configured margin free, clean temporary data, rotate logs/backups."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from ..config import Settings
from ..resources.governor import Governor

log = logging.getLogger("nextai.storage")


class DiskFull(Exception):
    pass


class StorageGuard:
    def __init__(self, settings: Settings, governor: Governor):
        self.settings, self.governor = settings, governor

    def free_gb(self) -> float:
        try:
            return shutil.disk_usage(self.settings.paths.data_dir).free / 2**30
        except OSError:
            return 0.0

    def ensure_can_write(self, add_bytes: int = 0, purpose: str = "") -> None:
        margin = self.settings.resources.disk_margin_gb
        if self.free_gb() - add_bytes / 2**30 < margin:
            raise DiskFull(f"ディスクの安全マージン({margin:.0f}GB)を維持するため{purpose or '書き込み'}を停止しました")

    def cleanup(self, aggressive: bool = False) -> dict[str, int]:
        p = self.settings.paths
        removed = {"tmp": 0, "backups": 0, "logs": 0, "workspaces": 0}
        max_age = self.settings.storage.tmp_max_age_hours * 3600 * (0.25 if aggressive else 1.0)
        cutoff = time.time() - max_age
        if p.tmp.exists():
            for entry in p.tmp.iterdir():
                try:
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()
                        removed["tmp"] += 1
                except OSError:
                    pass
        backups = sorted(p.backups.glob("backup-*.zip")) if p.backups.exists() else []
        keep = max(1, self.settings.storage.keep_backups - (4 if aggressive else 0))
        for b in backups[:-keep]:
            b.unlink(missing_ok=True)
            removed["backups"] += 1
        if p.logs.exists():
            for lf in p.logs.glob("*.log*"):
                try:
                    if lf.stat().st_size > 50 * 2**20:
                        _truncate_head(lf, 10 * 2**20)
                        removed["logs"] += 1
                except OSError:
                    pass
        if aggressive and p.users.exists():
            ws_cutoff = time.time() - 7 * 86400
            for ws in p.users.glob("*/workspaces/*"):
                try:
                    if ws.is_dir() and ws.stat().st_mtime < ws_cutoff:
                        shutil.rmtree(ws, ignore_errors=True)
                        removed["workspaces"] += 1
                except OSError:
                    pass
            for sb in p.users.glob("*/sandbox/*"):
                shutil.rmtree(sb, ignore_errors=True)
        if any(removed.values()):
            log.info("storage cleanup: %s", removed)
        return removed


def _truncate_head(path: Path, keep: int) -> None:
    with open(path, "rb") as f:
        f.seek(-keep, 2)
        tail = f.read()
    path.write_bytes(b"...(truncated)\n" + tail)
