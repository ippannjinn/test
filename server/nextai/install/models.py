"""Model set planning and installation into <data>/models/<model_id>/<component>/."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..db import Database
from ..models.catalog import Catalog
from ..util import dumps, now
from .downloader import Downloader, DownloadError, HFResolver, RemoteFile, ensure_space, make_client

Event = Callable[[dict], None]


def resolve_model(resolver: HFResolver, catalog: Catalog, model_id: str) -> dict[str, list[RemoteFile]]:
    spec = catalog.get(model_id)
    return {comp: resolver.resolve(sources) for comp, sources in spec.components.items()}


def _dest(models_dir: Path, model_id: str, comp: str, rf: RemoteFile) -> Path:
    return models_dir / model_id / comp / rf.name


def remaining_bytes(models_dir: Path, model_id: str, remote: dict[str, list[RemoteFile]]) -> int:
    total = 0
    for comp, files in remote.items():
        for rf in files:
            d = _dest(models_dir, model_id, comp, rf)
            if Downloader.verified(d, rf):
                continue
            part = d.with_name(d.name + ".part")
            total += max(0, (rf.size or 0) - (part.stat().st_size if part.exists() and not d.with_name(d.name + ".part.json").exists() else 0))
    return total


def mark_installed(db: Database, model_id: str, files: dict[str, str], nbytes: int) -> None:
    db.execute("INSERT INTO model_state(model_id, enabled, installed, files, bytes, installed_at) VALUES (?,1,1,?,?,?)"
               " ON CONFLICT(model_id) DO UPDATE SET installed=1, files=excluded.files, bytes=excluded.bytes,"
               " installed_at=excluded.installed_at", (model_id, dumps(files), nbytes, now()))


def install_models(settings: Settings, db: Database, catalog: Catalog, model_ids: list[str], emit: Event,
                   cancel: threading.Event | None = None) -> dict[str, str]:
    """Resolve everything first (so the disk check covers the whole set), then download sequentially."""
    m = settings.models
    client = make_client(m.hf_token or None)
    resolver = HFResolver(client, m.hf_endpoint)
    models_dir = settings.paths.models
    results: dict[str, str] = {}
    plan: dict[str, dict[str, list[RemoteFile]]] = {}
    for mid in model_ids:
        emit({"event": "resolve", "model": mid})
        try:
            plan[mid] = resolve_model(resolver, catalog, mid)
        except DownloadError as e:
            results[mid] = f"error: {e}"
            emit({"event": "model_error", "model": mid, "error": str(e)})
    need = sum(remaining_bytes(models_dir, mid, remote) for mid, remote in plan.items())
    total = sum(rf.size or 0 for remote in plan.values() for files in remote.values() for rf in files)
    emit({"event": "plan", "models": list(plan), "total_bytes": total, "remaining_bytes": need,
          "free_bytes": shutil.disk_usage(models_dir.parent if models_dir.exists() else settings.paths.data_dir).free})
    ensure_space(models_dir, need, settings.resources.disk_margin_gb)
    done_before = total - need
    progress_state = {"file_done": 0, "base": done_before}

    for mid, remote in plan.items():
        cur_file: dict[str, int] = {}

        def prog(name: str, done: int, size: int, mid=mid) -> None:
            prev = cur_file.get(name, 0)
            cur_file[name] = done
            progress_state["file_done"] += done - prev
            emit({"event": "progress", "model": mid, "file": name, "done": done, "size": size,
                  "overall_done": progress_state["base"] + progress_state["file_done"], "overall_total": total})

        dl = Downloader(client, prog, cancel=cancel)
        files: dict[str, str] = {}
        nbytes = 0
        try:
            for comp, rfs in remote.items():
                for rf in rfs:
                    was_verified = Downloader.verified(_dest(models_dir, mid, comp, rf), rf)
                    d = dl.download(rf, _dest(models_dir, mid, comp, rf))
                    if was_verified:
                        cur_file[rf.name] = rf.size or 0
                    nbytes += d.stat().st_size
                    files[comp] = (f"{mid}/{comp}" if len(rfs) > 1 or comp == "snapshot"
                                   else d.relative_to(models_dir).as_posix())
            mark_installed(db, mid, files, nbytes)
            results[mid] = "ok"
            emit({"event": "model_done", "model": mid, "bytes": nbytes})
        except DownloadError as e:
            results[mid] = f"error: {e}"
            emit({"event": "model_error", "model": mid, "error": str(e)})
            if "中断" in str(e):
                break
    client.close()
    return results


def remove_model_files(settings: Settings, db: Database, model_id: str) -> None:
    d = settings.paths.models / model_id
    if d.exists() and d.resolve().parent == settings.paths.models.resolve():
        shutil.rmtree(d)
    db.execute("UPDATE model_state SET installed=0, files='{}', bytes=0 WHERE model_id=?", (model_id,))
