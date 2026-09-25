from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..auth.deps import ApiError, Ctx, require_user

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
User = Annotated[Ctx, Depends(require_user)]


@router.get("")
def list_jobs(ctx: User, limit: int = 50):
    return {"jobs": ctx.p.jobs.list_for_user(ctx.uid, min(max(limit, 1), 200))}


@router.get("/{job_id}")
def get_job(job_id: str, ctx: User):
    rec = ctx.p.jobs.get_record(job_id, ctx.uid)
    if not rec:
        raise ApiError(404, "not_found", "ジョブが見つかりません")
    job = ctx.p.jobs.get(job_id, ctx.uid)
    rec["queue"] = ctx.p.jobs.queue_info(job) if job else None
    return {"job": rec}


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request, ctx: User, after: int = 0):
    job = ctx.p.jobs.get(job_id, ctx.uid)
    if job is None:
        rec = ctx.p.jobs.get_record(job_id, ctx.uid)
        if not rec:
            raise ApiError(404, "not_found", "ジョブが見つかりません")

        async def finished():
            yield f"event: done\ndata: {json.dumps({'status': rec['status'], 'error': rec['error'], 'result': rec['result']}, ensure_ascii=False)}\n\n"

        return StreamingResponse(finished(), media_type="text/event-stream")
    last = request.headers.get("last-event-id")
    if last and last.isdigit():
        after = max(after, int(last))

    async def gen():
        yield "retry: 2000\n\n"
        async for ev in ctx.p.jobs.stream(job, after):
            if await request.is_disconnected():
                return
            data = json.dumps(ev["data"], ensure_ascii=False, default=str)
            yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, ctx: User):
    if not ctx.p.jobs.cancel(job_id, ctx.uid):
        raise ApiError(404, "not_active", "実行中のジョブではありません")
    return {"ok": True}
