import asyncio
import time

import pytest

from nextai.backends import build_backends
from nextai.db import Database
from nextai.models.catalog import Catalog
from nextai.models.manager import HOT, ModelManager
from nextai.resources.governor import Governor
from nextai.resources.monitor import MockGpuProvider, ResourceMonitor
from nextai.scheduler import GpuScheduler, QueueTimeout, WorkUnit

from conftest import make_settings


@pytest.fixture
def sched_env(tmp_path):
    s = make_settings(tmp_path)
    s.paths.ensure()
    db = Database(s.paths.db)
    db.migrate()
    gpu = MockGpuProvider()
    gov = Governor(s)
    gov.evaluate(ResourceMonitor(s.paths.data_dir, gpu).sample())
    mgr = ModelManager(s, db, Catalog.load(), build_backends(s, gpu), gov)
    sch = GpuScheduler(s, mgr, gov)
    return s, mgr, sch, gov


def unit(user, model="qwen3-4b-instruct", cls="interactive", **kw):
    return WorkUnit(job_id="j", user_id=user, kind="llm", model_id=model, priority_class=cls, **kw)


async def test_micro_batching_shares_hot_model_slots(sched_env):
    s, mgr, sch, _ = sched_env
    sch.start()
    units = [unit(f"u{i}") for i in range(4)]
    await asyncio.gather(*(sch.acquire(u, 5) for u in units))
    assert all(u.state == "running" for u in units)
    assert mgr.runtimes["qwen3-4b-instruct"].in_use == 4
    for u in units:
        sch.release(u)
    await sch.stop()


async def test_user_concurrency_cap(sched_env):
    s, mgr, sch, _ = sched_env
    sch.start()
    a1, a2 = unit("a", user_concurrency=1), unit("a", user_concurrency=1)
    await sch.acquire(a1, 5)
    with pytest.raises(QueueTimeout):
        await sch.acquire(a2, 0.3)
    sch.release(a1)
    await sch.stop()


async def test_fair_share_prefers_light_user(sched_env):
    s, mgr, sch, _ = sched_env
    now = time.time()
    sch.usage["heavy"].extend([(now, 500.0)])
    h, l = unit("heavy"), unit("light")
    h.enqueued_at = l.enqueued_at = now
    assert sch._score(l, now, 2) > sch._score(h, now, 2)


async def test_aging_prevents_starvation(sched_env):
    s, mgr, sch, _ = sched_env
    now = time.time()
    old = unit("x", cls="batch", created_at=now - 120)
    new = unit("y", cls="interactive", created_at=now)
    old.enqueued_at, new.enqueued_at = now - 120, now
    assert sch._score(old, now, 2) > sch._score(new, now, 2)
    forced = unit("z", cls="batch", created_at=now - 500)
    forced.enqueued_at = now - 500
    assert sch._score(forced, now, 2) > 1000


async def test_swap_and_draining_for_waiting_unit(sched_env):
    s, mgr, sch, gov = sched_env
    s.set_override("scheduler.max_wait_force_seconds", 0.3)
    sch.start()
    small = unit("a", model="qwen3-30b-a3b-instruct")
    await sch.acquire(small, 5)
    video = WorkUnit(job_id="v", user_id="b", kind="video", model_id="wan21-t2v-1.3b", priority_class="batch")
    task = asyncio.create_task(sch.acquire(video, 10))
    await asyncio.sleep(0.6)
    # busy 30B MoE (~10GB) + video (9GB) exceed the budget → the forced unit drains the busy model
    assert "qwen3-30b-a3b-instruct" in sch.draining
    blocked = unit("c", model="qwen3-30b-a3b-instruct")
    t2 = asyncio.create_task(sch.acquire(blocked, 10))
    await asyncio.sleep(0.2)
    assert blocked.state == "pending"  # no new grants on a draining model
    sch.release(small)
    await asyncio.wait_for(task, 5)
    assert mgr.runtimes["wan21-t2v-1.3b"].state == HOT
    sch.release(video)
    await asyncio.wait_for(t2, 10)
    sch.release(blocked)
    await sch.stop()


async def test_position_and_eta(sched_env):
    s, mgr, sch, _ = sched_env
    sch.start()
    first = [unit(f"u{i}", user_concurrency=1) for i in range(4)]
    await asyncio.gather(*(sch.acquire(u, 5) for u in first))
    waiting = unit("u0", user_concurrency=1)
    t = asyncio.create_task(sch.acquire(waiting, 5))
    await asyncio.sleep(0.1)
    pos = sch.position(waiting.id)
    assert pos and pos[0] == 1 and pos[1] >= 0
    for u in first:
        sch.release(u)
    await asyncio.wait_for(t, 5)
    sch.release(waiting)
    await sch.stop()


async def test_governor_blocks_heavy_under_pressure(sched_env):
    s, mgr, sch, gov = sched_env
    gov.state.allow_heavy = False
    sch.start()
    img = WorkUnit(job_id="i", user_id="a", kind="image", model_id="flux1-schnell")
    with pytest.raises(QueueTimeout):
        await sch.acquire(img, 0.4)
    await sch.stop()
