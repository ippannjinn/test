"""Service container wiring every subsystem together."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable

from . import __version__
from .audit import AuditLog
from .auth.service import AuthService
from .backends import build_backends
from .backends.mock import hash_embedding
from .config import Settings
from .db import Database
from .jobs import JobManager
from .models.catalog import Catalog
from .models.manager import ModelManager
from .profile.engine import ProfileEngine
from .resources.governor import Governor, Level
from .resources.monitor import GpuProvider, ResourceMonitor, Snapshot, detect_gpu_provider
from .scheduler import GpuScheduler
from .security.ratelimit import RateLimiter
from .services.files import FileStore
from .services.memory import MemoryStore
from .services.storage import StorageGuard
from .tools.sandbox import SandboxManager
from .tools.web import WebClient
from .util import loads

log = logging.getLogger("nextai.platform")
RESTART_EXIT_CODE = 75


class ErrorBuffer(logging.Handler):
    def __init__(self, size: int = 200):
        super().__init__(level=logging.WARNING)
        self.records: deque[dict] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({"ts": record.created, "level": record.levelname, "logger": record.name,
                                 "message": record.getMessage()[:500]})
        except Exception:  # noqa: BLE001
            pass


class Platform:
    def __init__(self, settings: Settings, gpu: GpuProvider | None = None):
        self.settings = settings
        settings.paths.ensure()
        self.db = Database(settings.paths.db)
        self.db.migrate()
        self.reload_setting_overrides()
        self.audit = AuditLog(self.db)
        self.auth = AuthService(self.db, settings, self.audit)
        self.ratelimiter = RateLimiter()
        if gpu is None:
            mode = settings.resources.gpu_provider
            if mode == "auto" and settings.models.backend_mode == "mock":
                mode = "mock"
            gpu = detect_gpu_provider(mode)
        self.gpu = gpu
        self.monitor = ResourceMonitor(settings.paths.data_dir, gpu, settings.resources.monitor_interval_seconds)
        self.governor = Governor(settings)
        custom = [loads(r["custom_spec"]) for r in self.db.query("SELECT custom_spec FROM model_state WHERE custom_spec IS NOT NULL")]
        self.catalog = Catalog.load([c for c in custom if c])
        self.backends = build_backends(settings, gpu)
        self.models = ModelManager(settings, self.db, self.catalog, self.backends, self.governor)
        self.monitor.own_vram_fn = self.models.own_vram_mb
        self.scheduler = GpuScheduler(settings, self.models, self.governor)
        self.files = FileStore(settings, self.db)
        self.memory = MemoryStore(self.db, self.embed)
        self.storage = StorageGuard(settings, self.governor)
        self.sandbox = SandboxManager(settings, self.governor)
        self.web = WebClient(settings)
        self.profiles = ProfileEngine(settings, self.models, self.governor, self.scheduler.congestion,
                                      sandbox_enabled_fn=lambda: self.sandbox.available)
        self.jobs = JobManager(self)
        self.started_at = time.time()
        self.version = __version__
        self.exit_code = 0
        self.shutdown_cb: Callable[[], None] | None = None
        self.errors = ErrorBuffer()
        logging.getLogger("nextai").addHandler(self.errors)
        self._tasks: list[asyncio.Task] = []
        self.downloads: dict[str, dict[str, Any]] = {}

    def reload_setting_overrides(self) -> None:
        rows = self.db.query("SELECT key, value FROM settings")
        self.settings.load_overrides({r["key"]: loads(r["value"]) for r in rows})

    async def embed(self, texts: list[str]) -> tuple[str, list[list[float]]] | None:
        specs = self.models.usable_models(("embedding",))
        if not specs:
            if self.backends.mode == "mock":
                return "mock-hash", [hash_embedding(t) for t in texts]
            return None
        spec = specs[0]
        try:
            rt = await self.models.ensure_cpu_model(spec.id)
            return spec.id, await self.models.backend_for(spec).embed(rt.instance, texts)
        except Exception as e:  # noqa: BLE001 - memory falls back to lexical search
            log.warning("embedding unavailable: %s", e)
            return None

    def _on_snapshot(self, snap: Snapshot) -> None:
        prev = self.governor.state
        st = self.governor.evaluate(snap)
        if st.level != prev.level:
            (log.warning if st.level > prev.level else log.info)(
                "resource level %s -> %s: %s", prev.level.name, st.level.name, "; ".join(st.reasons) or "normal")
            self.scheduler.wake()
        if st.disk != "ok" and prev.disk == "ok":
            self._spawn(asyncio.to_thread(self.storage.cleanup, st.disk == "critical"))
        if st.level >= Level.HIGH and prev.level < Level.HIGH:
            self._spawn(self.models.maintenance({u.model_id for u in self.scheduler.running.values()}))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(t)
        t.add_done_callback(lambda x: self._tasks.remove(x) if x in self._tasks else None)

    async def _housekeeping(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.auth.cleanup)
                for u in await asyncio.to_thread(self.auth.users_due_for_purge):
                    await asyncio.to_thread(self.files.delete_all, u["id"])
                    await asyncio.to_thread(self.auth.mark_deleted, u["id"], actor=None, ip=None)
                await asyncio.to_thread(self.storage.cleanup, False)
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")
            await asyncio.sleep(600)

    async def start(self) -> None:
        snap = await asyncio.to_thread(self.monitor.sample)
        self.governor.evaluate(snap)
        self.monitor.listeners.append(self._on_snapshot)
        self.monitor.start()
        self.scheduler.start()
        self._spawn(self._housekeeping())
        log.info("platform started v%s backends=%s gpu=%s sandbox=%s", self.version, self.backends.mode,
                 self.gpu.name, self.sandbox.name)

    async def stop(self) -> None:
        await self.jobs.cancel_all()
        for t in list(self._tasks):
            t.cancel()
        await self.scheduler.stop()
        await self.models.shutdown()
        await self.monitor.stop()
        await self.web.close()
        logging.getLogger("nextai").removeHandler(self.errors)
        self.db.close()

    def request_restart(self) -> None:
        self.exit_code = RESTART_EXIT_CODE
        if self.shutdown_cb:
            self.shutdown_cb()
