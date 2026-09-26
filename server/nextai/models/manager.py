"""Model residency manager: VRAM (hot) / RAM page cache (warm) / NVMe (cold)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..backends import Backends
from ..backends.base import BackendError
from ..config import Settings
from ..db import Database
from ..resources.governor import Governor, Level
from ..util import dumps, loads, now
from .catalog import LLM_KINDS, Catalog, ModelSpec
from .planner import LaunchPlan, plan_llm, plan_media

log = logging.getLogger("nextai.models")

COLD, WARM, LOADING, HOT, UNLOADING, ERROR = "cold", "warm", "loading", "hot", "unloading", "error"


class ModelUnavailable(RuntimeError):
    pass


@dataclass(eq=False)
class ModelRuntime:
    spec: ModelSpec
    state: str = COLD
    instance: Any = None
    plan: LaunchPlan | None = None
    in_use: int = 0
    last_used: float = 0.0
    loaded_at: float = 0.0
    load_seconds: deque = field(default_factory=lambda: deque(maxlen=8))
    error: str = ""
    warm_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def expected_load_seconds(self) -> float:
        if self.load_seconds:
            return sum(self.load_seconds) / len(self.load_seconds)
        base = 3 + self.spec.size_gb * 1.2
        return base * (0.4 if self.warm_until > time.time() else 1.0)


class ModelManager:
    def __init__(self, settings: Settings, db: Database, catalog: Catalog, backends: Backends, governor: Governor):
        self.settings, self.db, self.catalog, self.backends, self.governor = settings, db, catalog, backends, governor
        self.runtimes: dict[str, ModelRuntime] = {mid: ModelRuntime(spec) for mid, spec in catalog.models.items()}
        self.load_events: deque[float] = deque(maxlen=200)
        self.swap_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._prefetch_tasks: dict[str, asyncio.Task] = {}
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ install state
    def _row(self, model_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM model_state WHERE model_id=?", (model_id,))

    def add_custom_spec(self, spec: ModelSpec, raw: dict[str, Any]) -> None:
        self.catalog.models[spec.id] = spec
        self.runtimes.setdefault(spec.id, ModelRuntime(spec))
        self.db.execute("INSERT INTO model_state(model_id, enabled, custom_spec) VALUES (?,1,?)"
                        " ON CONFLICT(model_id) DO UPDATE SET custom_spec=excluded.custom_spec", (spec.id, dumps(raw)))

    def paths(self, model_id: str) -> dict[str, Path] | None:
        row = self._row(model_id)
        if not row or not row["installed"]:
            return None
        files = loads(row["files"], {})
        out = {}
        for comp, rel in files.items():
            p = (self.settings.paths.models / rel).resolve()
            if not str(p).startswith(str(self.settings.paths.models.resolve())):
                return None
            out[comp] = p
        return out

    def is_installed(self, model_id: str) -> bool:
        if self.backends.mode == "mock":
            return True
        p = self.paths(model_id)
        return bool(p) and all(x.exists() for x in p.values())

    def is_enabled(self, model_id: str) -> bool:
        row = self._row(model_id)
        return True if row is None else bool(row["enabled"])

    def set_enabled(self, model_id: str, enabled: bool) -> None:
        if model_id not in self.catalog.models:
            raise KeyError(model_id)
        self.db.execute("INSERT INTO model_state(model_id, enabled) VALUES (?,?)"
                        " ON CONFLICT(model_id) DO UPDATE SET enabled=excluded.enabled", (model_id, 1 if enabled else 0))

    def mark_installed(self, model_id: str, files: dict[str, str], total_bytes: int) -> None:
        self.db.execute(
            "INSERT INTO model_state(model_id, enabled, installed, files, bytes, installed_at) VALUES (?,1,1,?,?,?)"
            " ON CONFLICT(model_id) DO UPDATE SET installed=1, files=excluded.files, bytes=excluded.bytes,"
            " installed_at=excluded.installed_at", (model_id, dumps(files), total_bytes, now()))

    def mark_removed(self, model_id: str) -> None:
        self.db.execute("UPDATE model_state SET installed=0, files='{}', bytes=0 WHERE model_id=?", (model_id,))

    def correction(self, model_id: str) -> float:
        row = self._row(model_id)
        cal = loads(row["calibration"], {}) if row else {}
        return float(cal.get("vram_correction", 1.0))

    def record_calibration(self, model_id: str, **values: Any) -> None:
        row = self._row(model_id)
        cal = loads(row["calibration"], {}) if row else {}
        cal.update(values)
        self.db.execute("INSERT INTO model_state(model_id, calibration) VALUES (?,?)"
                        " ON CONFLICT(model_id) DO UPDATE SET calibration=excluded.calibration", (model_id, dumps(cal)))

    def backend_for(self, spec: ModelSpec):
        return self.backends.for_spec_backend(spec.backend, spec.kind)

    def usable(self, model_id: str) -> bool:
        spec = self.catalog.models.get(model_id)
        if not spec or not self.is_enabled(model_id):
            return False
        if not self.backend_for(spec).available:
            return False
        return self.is_installed(model_id)

    def usable_models(self, kinds: tuple[str, ...] | None = None) -> list[ModelSpec]:
        return [s for mid, s in self.catalog.models.items() if (kinds is None or s.kind in kinds) and self.usable(mid)]

    # ------------------------------------------------------------------ VRAM accounting
    def own_vram_mb(self) -> int:
        return sum(rt.plan.est_vram_mb for rt in self.runtimes.values()
                   if rt.state in (HOT, LOADING, UNLOADING) and rt.plan and rt.plan.gpu)

    def runtime(self, model_id: str) -> ModelRuntime:
        return self.runtimes[model_id]

    def _file_mb(self, spec: ModelSpec) -> float:
        paths = self.paths(spec.id) if self.backends.mode != "mock" else None
        if paths and "model" in paths and paths["model"].exists():
            return paths["model"].stat().st_size / 2**20
        return spec.size_gb * 1024 * (0.8 if "mmproj" in spec.components else 1.0)

    def make_plan(self, model_id: str, vram_free_mb: float) -> LaunchPlan | None:
        spec = self.catalog.get(model_id)
        gs = self.governor.state
        corr = self.correction(model_id)
        if spec.kind in LLM_KINDS or spec.kind == "embedding":
            ctx = spec.defaults.get("ctx", 8192)
            parallel = min(spec.defaults.get("parallel", 1), self.settings.models.llm_parallel)
            return plan_llm(spec, self._file_mb(spec), vram_budget_mb=vram_free_mb, ram_budget_mb=gs.ram_budget_mb,
                            ctx=ctx, parallel=parallel, kv_type=self.settings.models.kv_cache_type,
                            correction=corr, has_gpu=gs.has_gpu,
                            ram_overcommit=float(self.settings.models.moe_ram_overcommit))
        return plan_media(spec, vram_budget_mb=vram_free_mb, correction=corr, has_gpu=gs.has_gpu)

    @staticmethod
    def _acceptable(spec: ModelSpec, plan: LaunchPlan | None) -> bool:
        if plan is None:
            return False
        if not plan.gpu:
            return True
        if spec.moe:
            return plan.n_cpu_moe <= spec.arch.get("n_layers", 48) * 0.75
        return plan.n_gpu_layers >= 999 and not plan.offload

    def fits_now(self, model_id: str) -> bool:
        """Loaded already, or loadable within the current VRAM + free-RAM budget (evicting idle models)."""
        rt = self.runtimes.get(model_id)
        if rt is not None and rt.state == HOT:
            return True
        try:
            return self.can_load(model_id)[0] != "impossible"
        except Exception:  # noqa: BLE001 - be permissive; loading will report the real error
            return True

    def ram_heavy(self, model_id: str) -> bool:
        rt = self.runtimes.get(model_id)
        return bool(rt and rt.plan and rt.plan.est_ram_mb > 1024)

    def can_load(self, model_id: str) -> tuple[str, list[str], LaunchPlan | None]:
        """Returns ("ok", evict, plan) | ("blocked", busy_ids, None) | ("impossible", [], None)."""
        spec = self.catalog.get(model_id)
        budget = self.governor.state.vram_budget_mb
        others = [rt for rt in self.runtimes.values() if rt.spec.id != model_id and rt.state in (HOT, LOADING, UNLOADING)
                  and rt.plan and rt.plan.gpu]
        used = sum(rt.plan.est_vram_mb for rt in others)
        plan = self.make_plan(model_id, budget - used)
        if plan and (not plan.gpu or self._acceptable(spec, plan)):
            return "ok", [], plan
        resident = self._resident_id()
        idle = sorted((rt for rt in others if rt.in_use == 0 and rt.state == HOT),
                      key=lambda rt: (rt.spec.id == resident, rt.last_used))
        evict: list[str] = []
        best = plan
        for rt in idle:
            evict.append(rt.spec.id)
            used -= rt.plan.est_vram_mb
            candidate = self.make_plan(model_id, budget - used)
            if candidate:
                best = candidate
                if self._acceptable(spec, candidate):
                    return "ok", evict, candidate
        if best is not None:
            return "ok", evict if best is not plan else [], best
        busy = [rt for rt in others if rt.spec.id not in evict]
        freeable = sum(rt.plan.est_vram_mb for rt in busy)
        if busy and self.make_plan(model_id, budget - used + freeable) is not None:
            return "blocked", [rt.spec.id for rt in busy], None
        return "impossible", [], None

    def slots(self, model_id: str) -> int:
        rt = self.runtimes[model_id]
        if rt.spec.kind in ("image", "video", "music"):
            return 1
        base = rt.plan.parallel if rt.plan else 1
        return max(1, int(base * self.governor.state.llm_parallel_factor))

    def thrashing(self) -> bool:
        m = self.settings.models
        cutoff = time.time() - m.thrash_window_seconds
        return sum(1 for t in self.load_events if t >= cutoff) > m.thrash_max_swaps

    def _resident_id(self) -> str | None:
        if not self.settings.models.resident_fast_model:
            return None
        for s in self.usable_models(("llm",)):
            if "fast" in s.roles:
                return s.id
        return None

    # ------------------------------------------------------------------ load / unload
    async def load(self, model_id: str, evict: list[str], plan: LaunchPlan, reason: str = "") -> ModelRuntime:
        rt = self.runtimes[model_id]
        async with rt.lock:
            media = rt.spec.kind in ("image", "video", "music")
            if rt.state == HOT and (media or self.backend_for(rt.spec).alive(rt.instance)):
                return rt
            for mid in evict:
                await self.unload(mid, f"swap for {model_id}")
            if not self.usable(model_id):
                raise ModelUnavailable(f"モデル {rt.spec.display_name} は利用できません (未インストールまたは無効)")
            rt.state, rt.plan, rt.error = LOADING, plan, ""
            t0 = time.time()
            try:
                if rt.spec.kind in ("image", "video", "music"):
                    rt.instance = None
                else:
                    paths = self.paths(model_id) or {}
                    rt.instance = await self.backend_for(rt.spec).start(rt.spec, paths, plan)
            except BaseException as e:
                rt.state, rt.plan, rt.instance = ERROR, None, None
                rt.error = str(e)[:500]
                log.error("load %s failed: %s", model_id, e)
                if isinstance(e, BackendError):
                    raise ModelUnavailable(str(e)) from e
                raise
            dt = time.time() - t0
            rt.state, rt.loaded_at, rt.last_used = HOT, time.time(), time.time()
            if rt.spec.kind not in ("image", "video", "music"):
                rt.load_seconds.append(dt)
                self.load_events.append(time.time())
            self.swap_log.append({"ts": time.time(), "loaded": model_id, "evicted": evict, "seconds": round(dt, 2),
                                  "reason": reason, "plan": plan.to_dict()})
            log.info("loaded %s in %.1fs plan=%s evicted=%s", model_id, dt, plan.notes, evict)
            return rt

    def reserve_load(self, model_id: str, evict: list[str]) -> None:
        """Synchronously claim state so no unit is granted on models about to be evicted."""
        for mid in evict:
            rt = self.runtimes[mid]
            if rt.state == HOT and rt.in_use == 0:
                rt.state = UNLOADING
        self.runtimes[model_id].state = LOADING

    async def unload(self, model_id: str, reason: str = "") -> None:
        rt = self.runtimes[model_id]
        if rt.state not in (HOT, ERROR, UNLOADING):
            return
        rt.state = UNLOADING
        try:
            if rt.instance is not None:
                await self.backend_for(rt.spec).stop(rt.instance)
        finally:
            rt.instance, rt.state, rt.plan = None, COLD, None
            if rt.spec.kind not in ("image", "video", "music"):
                log.info("unloaded %s (%s)", model_id, reason)

    async def ensure_cpu_model(self, model_id: str) -> ModelRuntime:
        """CPU-only models (embeddings) bypass the GPU scheduler."""
        rt = self.runtimes[model_id]
        if rt.state == HOT and self.backend_for(rt.spec).alive(rt.instance):
            return rt
        plan = self.make_plan(model_id, 0)
        if plan is None:
            raise ModelUnavailable(model_id)
        return await self.load(model_id, [], plan, "cpu model")

    def mark_used(self, model_id: str) -> None:
        self.runtimes[model_id].last_used = time.time()

    # ------------------------------------------------------------------ warm tier
    def prefetch(self, model_id: str) -> None:
        rt = self.runtimes[model_id]
        if (model_id in self._prefetch_tasks or rt.state != COLD or rt.warm_until > time.time()
                or self.backends.mode == "mock" or not self.governor.state.allow_prefetch or self.thrashing()):
            return
        paths = self.paths(model_id)
        if not paths:
            return
        total = sum(p.stat().st_size for p in paths.values() if p.exists())
        if total / 2**20 > self.governor.state.ram_budget_mb * 0.8:
            return

        def read_all() -> None:
            for p in paths.values():
                with open(p, "rb") as f:
                    while self.governor.state.allow_prefetch and f.read(16 * 2**20):
                        pass

        async def run():
            try:
                await asyncio.to_thread(read_all)
                if self.governor.state.allow_prefetch:
                    rt.warm_until = time.time() + 600
                    if rt.state == COLD:
                        rt.state = WARM
            except OSError as e:
                log.debug("prefetch %s failed: %s", model_id, e)
            finally:
                self._prefetch_tasks.pop(model_id, None)

        self._prefetch_tasks[model_id] = asyncio.get_running_loop().create_task(run())

    # ------------------------------------------------------------------ housekeeping
    async def maintenance(self, pending_models: set[str]) -> None:
        ts = time.time()
        level = self.governor.state.level
        resident = self._resident_id()
        for mid, rt in self.runtimes.items():
            if rt.state == WARM and rt.warm_until < ts:
                rt.state = COLD
            if rt.state == HOT and rt.instance is not None and not self.backend_for(rt.spec).alive(rt.instance):
                log.warning("model process %s died", mid)
                rt.state, rt.instance, rt.plan, rt.error = ERROR, None, None, "プロセスが終了しました"
                continue
            if rt.state != HOT or rt.in_use > 0 or mid in pending_models:
                continue
            idle = ts - rt.last_used
            if rt.spec.kind in ("image", "video", "music"):
                await self.unload(mid, "transient")
            elif rt.spec.kind == "embedding":
                continue
            elif level >= Level.CRITICAL:
                await self.unload(mid, "resource critical")
            elif level >= Level.HIGH and mid != resident:
                await self.unload(mid, "resource pressure")
            elif idle > self.settings.models.idle_unload_seconds and mid != resident:
                await self.unload(mid, "idle")
            elif self.governor.state.vram_budget_mb < self.own_vram_mb() and mid != resident:
                await self.unload(mid, "vram budget shrank")
        if level == Level.NORMAL and resident and not pending_models and self.runtimes[resident].state in (COLD, WARM):
            status, evict, plan = self.can_load(resident)
            if status == "ok" and not evict and plan:
                self._spawn(self.load(resident, [], plan, "resident warm start"))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(lambda task: (self._bg.discard(task), task.exception() if not task.cancelled() else None))

    async def shutdown(self) -> None:
        for t in list(self._prefetch_tasks.values()) + list(self._bg):
            t.cancel()
        for mid, rt in self.runtimes.items():
            if rt.state in (HOT, ERROR):
                try:
                    await self.unload(mid, "shutdown")
                except Exception:  # noqa: BLE001
                    log.exception("unload %s failed", mid)

    def status(self) -> list[dict[str, Any]]:
        out = []
        for mid, rt in self.runtimes.items():
            spec = rt.spec
            row = self._row(mid)
            out.append({
                "id": mid, "display_name": spec.display_name, "kind": spec.kind, "roles": spec.roles,
                "backend": spec.backend, "license": spec.license, "size_gb": spec.size_gb, "custom": spec.custom,
                "installed": self.is_installed(mid), "enabled": self.is_enabled(mid), "usable": self.usable(mid),
                "state": rt.state, "in_use": rt.in_use, "error": rt.error,
                "plan": rt.plan.to_dict() if rt.plan else None,
                "loaded_at": rt.loaded_at or None, "last_used": rt.last_used or None,
                "expected_load_seconds": round(rt.expected_load_seconds, 1),
                "calibration": loads(row["calibration"], {}) if row else {},
                "bytes": row["bytes"] if row else 0,
            })
        return out
