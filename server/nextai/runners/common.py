"""Shared helpers: scheduled LLM calls with live queue feedback."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..backends.base import ChatRequest
from ..jobs import Job
from ..models.manager import HOT, LOADING, ModelUnavailable
from ..profile.engine import Profile
from ..scheduler import WorkUnit


@dataclass
class LLMResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""


async def _watch_queue(p: Any, job: Job, unit: WorkUnit) -> None:
    last = None
    while unit.state in ("new", "pending"):
        pos = p.scheduler.position(unit.id)
        rt = p.models.runtimes[unit.model_id]
        info = {"model_id": unit.model_id, "model_state": rt.state}
        if pos:
            info.update(position=pos[0], eta_seconds=pos[1])
        key = (info.get("position"), rt.state, int(info.get("eta_seconds", 0) // 5))
        if key != last:
            job.emit("queue", **info)
            last = key
        await asyncio.sleep(1.0)


@asynccontextmanager
async def gpu_lease(p: Any, job: Job, user: dict, profile: Profile, kind: str, model_id: str,
                    est_seconds: float) -> AsyncIterator[WorkUnit]:
    unit = WorkUnit(job_id=job.id, user_id=user["id"], kind=kind, model_id=model_id,
                    priority_class=profile.priority_class, user_priority=int(user["queue_priority"]),
                    user_concurrency=int(user["concurrent_jobs"]),
                    est_seconds=p.scheduler.estimate_seconds(kind, model_id, est_seconds),
                    created_at=job.created_at, quality_pinned=profile.quality_pinned)
    job.current_unit = unit.id
    watcher = asyncio.get_running_loop().create_task(_watch_queue(p, job, unit))
    try:
        await p.scheduler.acquire(unit)
    finally:
        watcher.cancel()
    job.emit("queue", model_id=model_id, model_state=HOT, position=0, eta_seconds=0, started=True)
    t0 = time.time()
    try:
        yield unit
    finally:
        p.scheduler.release(unit)
        job.gpu_seconds += time.time() - t0
        job.current_unit = None


async def llm_call(p: Any, job: Job, user: dict, profile: Profile, messages: list[dict], *,
                   tools: list[dict] | None = None, max_tokens: int | None = None, stream: bool = True,
                   json_mode: bool = False, temperature: float | None = None) -> LLMResult:
    if not profile.model_id:
        raise ModelUnavailable("利用可能な言語モデルがありません。管理者にモデルのインストールを依頼してください")
    spec = p.catalog.get(profile.model_id)
    est = max(4.0, (max_tokens or profile.max_tokens) / 40.0)
    res = LLMResult()
    async with gpu_lease(p, job, user, profile, spec.kind, spec.id, est):
        rt = p.models.runtimes[spec.id]
        if rt.state != HOT or rt.instance is None:
            raise ModelUnavailable(f"{spec.display_name} が利用できません")
        backend = p.models.backend_for(spec)
        req = ChatRequest(messages=messages, max_tokens=max_tokens or profile.max_tokens,
                          temperature=profile.temperature if temperature is None else temperature,
                          tools=tools or None, reasoning=profile.reasoning, json_mode=json_mode)
        async for ev in backend.chat(rt.instance, spec, req):
            if ev.type == "content":
                res.content += ev.text
                if stream:
                    job.emit("delta", text=ev.text)
            elif ev.type == "reasoning":
                res.reasoning += ev.text
                if stream:
                    job.emit("reasoning", text=ev.text)
            elif ev.type == "tool_calls":
                res.tool_calls = ev.tool_calls
            elif ev.type == "done":
                res.usage, res.timings, res.finish_reason = ev.usage, ev.timings, ev.finish_reason or ""
    tokens = int(res.usage.get("completion_tokens", 0) or 0) + int(res.usage.get("prompt_tokens", 0) or 0)
    if tokens:
        p.jobs.record_usage(user["id"], tokens=tokens)
    return res
