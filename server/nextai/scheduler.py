"""GPU scheduler shared by dozens of users: fairness + responsiveness + GPU efficiency.

Policy (not FIFO):
  score = class weight + user priority + aging − fair-share penalty ± model affinity (hot vs swap)
  * aging grows without bound → heavy/low-priority work can never wait forever
  * units waiting longer than `max_wait_force_seconds` get absolute precedence and may drain a busy model
  * per-user concurrency caps and a decayed GPU-time share stop any one user monopolising the GPU
  * units for an already-hot model share its slots (llama.cpp continuous batching = micro-batching)
  * swaps are delayed by a patience window while hot-model work is available (swap minimisation)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from .config import Settings
from .models.manager import COLD, ERROR, HOT, LOADING, UNLOADING, WARM, ModelManager, ModelUnavailable
from .resources.governor import Governor
from .util import new_id

log = logging.getLogger("nextai.scheduler")

CLASS_WEIGHT = {"interactive": 100.0, "standard": 60.0, "batch": 30.0}
HEAVY_KINDS = ("image", "video", "music")


class QueueTimeout(Exception):
    pass


@dataclass(eq=False)
class WorkUnit:
    job_id: str
    user_id: str
    kind: str
    model_id: str
    priority_class: str = "standard"
    user_priority: int = 0
    user_concurrency: int = 2
    est_seconds: float = 10.0
    created_at: float = field(default_factory=time.time)
    quality_pinned: bool = False
    id: str = field(default_factory=new_id)
    enqueued_at: float = 0.0
    granted_at: float = 0.0
    state: str = "new"
    future: asyncio.Future | None = None

    @property
    def heavy(self) -> bool:
        return self.kind in HEAVY_KINDS


class GpuScheduler:
    def __init__(self, settings: Settings, manager: ModelManager, governor: Governor):
        self.settings, self.manager, self.governor = settings, manager, governor
        self.pending: list[WorkUnit] = []
        self.running: dict[str, WorkUnit] = {}
        self.usage: dict[str, deque] = defaultdict(deque)
        self.durations: dict[str, float] = {}
        self.draining: set[str] = set()
        self.failed_loads: dict[str, tuple[float, str]] = {}
        self.completed = 0
        self._loading: asyncio.Task | None = None
        self._loading_model: str | None = None
        self._maint: asyncio.Task | None = None
        self._last_maint = 0.0
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="gpu-scheduler")

    async def stop(self) -> None:
        for t in (self._task, self._loading, self._maint):
            if t:
                t.cancel()
        for u in list(self.pending):
            if u.future and not u.future.done():
                u.future.set_exception(asyncio.CancelledError())
        self.pending.clear()

    def wake(self) -> None:
        if self._wake:
            self._wake.set()

    async def _loop(self) -> None:
        tick = self.settings.scheduler.tick_seconds
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                self._dispatch()
            except Exception:  # noqa: BLE001
                log.exception("dispatch failed")
            ts = time.time()
            if ts - self._last_maint > 5 and (self._maint is None or self._maint.done()) and self._loading is None:
                self._last_maint = ts
                busy = {u.model_id for u in self.pending} | {u.model_id for u in self.running.values()}
                self._maint = asyncio.get_running_loop().create_task(self.manager.maintenance(busy))

    # ------------------------------------------------------------------ public API
    async def acquire(self, unit: WorkUnit, timeout: float | None = None) -> WorkUnit:
        if unit.model_id not in self.manager.runtimes:
            raise ModelUnavailable(f"unknown model {unit.model_id}")
        unit.future = asyncio.get_running_loop().create_future()
        unit.enqueued_at, unit.state = time.time(), "pending"
        self.pending.append(unit)
        self.wake()
        timeout = timeout or self.settings.scheduler.queue_timeout_seconds
        try:
            await asyncio.wait_for(asyncio.shield(unit.future), timeout)
        except asyncio.TimeoutError:
            if unit.state == "running":
                return unit
            self._remove(unit)
            raise QueueTimeout("キューの待ち時間が上限を超えました") from None
        except asyncio.CancelledError:
            if unit.state == "running":
                self.release(unit)
            else:
                self._remove(unit)
            raise
        return unit

    def release(self, unit: WorkUnit) -> None:
        if self.running.pop(unit.id, None) is None:
            return
        rt = self.manager.runtimes.get(unit.model_id)
        if rt:
            rt.in_use = max(0, rt.in_use - 1)
            rt.last_used = time.time()
        dur = time.time() - unit.granted_at
        self.usage[unit.user_id].append((time.time(), dur))
        key = f"{unit.kind}:{unit.model_id}"
        prev = self.durations.get(key)
        self.durations[key] = dur if prev is None else prev * 0.7 + dur * 0.3
        unit.state = "done"
        self.completed += 1
        self.wake()

    @asynccontextmanager
    async def lease(self, unit: WorkUnit, timeout: float | None = None) -> AsyncIterator[WorkUnit]:
        await self.acquire(unit, timeout)
        try:
            yield unit
        finally:
            self.release(unit)

    def estimate_seconds(self, kind: str, model_id: str, default: float) -> float:
        return self.durations.get(f"{kind}:{model_id}", default)

    def position(self, unit_id: str) -> tuple[int, float] | None:
        now = time.time()
        ordered = self._ordered(now)
        for i, u in enumerate(ordered):
            if u.id == unit_id:
                return i + 1, round(self._eta(u, ordered[:i]), 1)
        return None

    def _eta(self, unit: WorkUnit, ahead: list[WorkUnit]) -> float:
        rt = self.manager.runtimes[unit.model_id]
        slots = max(1, self.manager.slots(unit.model_id)) if rt.state == HOT else 1
        same = sum(u.est_seconds for u in ahead if u.model_id == unit.model_id) / slots
        other = sum(u.est_seconds for u in ahead if u.model_id != unit.model_id)
        load = 0.0 if rt.state == HOT else rt.expected_load_seconds
        busy = [u for u in self.running.values() if u.model_id == unit.model_id]
        wait_slot = 0.0
        if rt.state == HOT and len(busy) >= slots:
            wait_slot = min(max(0.0, u.est_seconds - (time.time() - u.granted_at)) for u in busy)
        return same + other + load + wait_slot

    def stats(self) -> dict:
        now = time.time()
        ordered = self._ordered(now)
        wait = self._eta(ordered[-1], ordered[:-1]) if ordered else 0.0
        by_class: dict[str, int] = defaultdict(int)
        for u in self.pending:
            by_class[u.priority_class] += 1
        return {
            "pending": len(self.pending), "running": len(self.running), "completed": self.completed,
            "by_class": dict(by_class), "loading_model": self._loading_model, "draining": sorted(self.draining),
            "est_wait_seconds": round(wait, 1), "congestion": round(self.congestion(), 3),
            "thrashing": self.manager.thrashing(),
        }

    def congestion(self) -> float:
        if not self.pending:
            return min(0.3, len(self.running) * 0.05)
        work = sum(u.est_seconds for u in self.pending)
        slots = max(1, sum(self.manager.slots(m) for m, rt in self.manager.runtimes.items() if rt.state == HOT) or 1)
        return max(0.0, min(1.0, work / slots / 90.0))

    def snapshot(self) -> list[dict]:
        now = time.time()
        rows = []
        for i, u in enumerate(self._ordered(now)):
            rows.append(self._row(u, now, "pending", i + 1))
        for u in self.running.values():
            rows.append(self._row(u, now, "running", 0))
        return rows

    def _row(self, u: WorkUnit, now: float, state: str, pos: int) -> dict:
        return {"unit_id": u.id, "job_id": u.job_id, "user_id": u.user_id, "kind": u.kind, "model_id": u.model_id,
                "priority_class": u.priority_class, "user_priority": u.user_priority, "state": state, "position": pos,
                "waited_seconds": round((u.granted_at or now) - u.enqueued_at, 1),
                "running_seconds": round(now - u.granted_at, 1) if state == "running" else 0.0,
                "est_seconds": round(u.est_seconds, 1)}

    # ------------------------------------------------------------------ policy
    def _user_gpu_seconds(self, user_id: str, now: float) -> float:
        window = self.settings.scheduler.fair_share_window_seconds
        q = self.usage[user_id]
        while q and q[0][0] < now - window:
            q.popleft()
        used = sum(s for _, s in q)
        used += sum(now - u.granted_at for u in self.running.values() if u.user_id == user_id)
        return used

    def _score(self, u: WorkUnit, now: float, active_users: int) -> float:
        s_cfg = self.settings.scheduler
        s = CLASS_WEIGHT.get(u.priority_class, 60.0) + 10.0 * u.user_priority
        s += (now - u.created_at) * s_cfg.aging_per_second
        fair = s_cfg.fair_share_window_seconds / max(1, active_users)
        s -= s_cfg.fair_share_penalty * min(1.0, self._user_gpu_seconds(u.user_id, now) / max(1.0, fair))
        rt = self.manager.runtimes[u.model_id]
        if rt.state == HOT:
            s += s_cfg.hot_model_bonus
        else:
            s -= min(20.0, rt.expected_load_seconds * 0.5)
        if u.heavy:
            s -= 10.0
        if now - u.enqueued_at > s_cfg.max_wait_force_seconds:
            s += 1000.0
        return s

    def _ordered(self, now: float) -> list[WorkUnit]:
        active = len({u.user_id for u in self.pending} | {u.user_id for u in self.running.values()})
        return sorted(self.pending, key=lambda u: self._score(u, now, active), reverse=True)

    def _user_running(self, user_id: str) -> int:
        return sum(1 for u in self.running.values() if u.user_id == user_id)

    def _swap_patience(self) -> float:
        base = float(self.settings.scheduler.swap_patience_seconds)
        return base * 3 if self.manager.thrashing() else base

    def _grant(self, u: WorkUnit) -> None:
        rt = self.manager.runtimes[u.model_id]
        rt.in_use += 1
        rt.last_used = time.time()
        u.state, u.granted_at = "running", time.time()
        self.pending.remove(u)
        self.running[u.id] = u
        if u.future and not u.future.done():
            u.future.set_result(u)

    def _fail(self, u: WorkUnit, exc: Exception) -> None:
        if u in self.pending:
            self.pending.remove(u)
        u.state = "failed"
        if u.future and not u.future.done():
            u.future.set_exception(exc)

    def _remove(self, u: WorkUnit) -> None:
        if u in self.pending:
            self.pending.remove(u)
        u.state = "cancelled"
        self.wake()

    def _start_load(self, model_id: str, evict: list[str], plan) -> None:
        self.manager.reserve_load(model_id, evict)
        self._loading_model = model_id
        self.draining.clear()

        async def run() -> None:
            try:
                await self.manager.load(model_id, evict, plan, reason="scheduler")
                self.failed_loads.pop(model_id, None)
            except Exception as e:  # noqa: BLE001
                self.failed_loads[model_id] = (time.time(), str(e))
                for u in [u for u in self.pending if u.model_id == model_id]:
                    self._fail(u, e if isinstance(e, ModelUnavailable) else ModelUnavailable(str(e)))
            finally:
                self._loading, self._loading_model = None, None
                self.wake()

        self._loading = asyncio.get_running_loop().create_task(run())

    def _dispatch(self) -> None:
        now = time.time()
        gs = self.governor.state
        self.pending = [u for u in self.pending if u.future is not None and not u.future.done()]
        if not self.pending:
            self.draining.clear()
            return
        ordered = self._ordered(now)
        force_wait = self.settings.scheduler.max_wait_force_seconds
        needs_swap = [u for u in ordered if self.manager.runtimes[u.model_id].state not in (HOT, LOADING)]
        if not needs_swap:
            self.draining.clear()
        force_unit = next((u for u in needs_swap if now - u.enqueued_at > force_wait), None)
        hot_waiting = any(self.manager.runtimes[u.model_id].state == HOT for u in ordered)

        def admissible(u: WorkUnit) -> bool:
            return (u.state == "pending" and self._user_running(u.user_id) < u.user_concurrency
                    and (gs.accept_jobs or u.priority_class == "interactive") and (gs.allow_heavy or not u.heavy))

        # Pass 1: grant work on already-hot models first, so a model loaded for a waiting unit is
        # never evicted by another unit's swap before that unit gets its slot.
        for u in ordered:
            if not admissible(u):
                continue
            rt = self.manager.runtimes[u.model_id]
            if rt.state == HOT and u.model_id not in self.draining and rt.in_use < self.manager.slots(u.model_id):
                self._grant(u)
        # Pass 2: at most one swap/load decision.
        for u in ordered:
            if not admissible(u):
                continue
            rt = self.manager.runtimes[u.model_id]
            if rt.state == HOT:
                continue
            if rt.state in (LOADING, UNLOADING) or self._loading is not None:
                continue
            if force_unit is not None and u is not force_unit:
                continue
            waited = now - u.enqueued_at
            if u is not force_unit and hot_waiting and waited < self._swap_patience():
                if gs.allow_prefetch and rt.state == COLD:
                    self.manager.prefetch(u.model_id)
                continue
            failed = self.failed_loads.get(u.model_id)
            if failed and now - failed[0] < 30:
                self._fail(u, ModelUnavailable(failed[1]))
                continue
            status, ids, plan = self.manager.can_load(u.model_id)
            if status == "ok":
                self._start_load(u.model_id, ids, plan)
            elif status == "blocked":
                if u is force_unit or waited >= self._swap_patience():
                    self.draining = set(ids)
                if gs.allow_prefetch and rt.state in (COLD, ERROR):
                    self.manager.prefetch(u.model_id)
            else:
                self._fail(u, ModelUnavailable("現在のVRAM/RAMではこのモデルを読み込めません"))
