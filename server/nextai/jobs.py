"""Job lifecycle, admission control and resumable event streams."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from .util import day_key, dumps, loads, new_id, now

log = logging.getLogger("nextai.jobs")

ACTIVE = ("queued", "running")
MAX_EVENTS = 6000


class AdmissionError(Exception):
    def __init__(self, code: str, message: str, status: int = 429):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Job:
    def __init__(self, job_id: str, user_id: str, kind: str, conversation_id: str | None, request: dict):
        self.id, self.user_id, self.kind, self.conversation_id, self.request = job_id, user_id, kind, conversation_id, request
        self.status = "queued"
        self.created_at, self.started_at, self.finished_at = now(), None, None
        self.profile: dict = {}
        self.result: dict = {}
        self.error: str | None = None
        self.events: list[dict] = []
        self.seq = 0
        self.text = ""
        self.task: asyncio.Task | None = None
        self.cancel_event = threading.Event()
        self.current_unit: str | None = None
        self.gpu_seconds = 0.0
        self._notify = asyncio.Event()

    def emit(self, type_: str, **data: Any) -> None:
        self.seq += 1
        if type_ == "delta":
            self.text += data.get("text", "")
        elif type_ == "reset":
            self.text = ""
        self.events.append({"seq": self.seq, "type": type_, "ts": time.time(), "data": data})
        if len(self.events) > MAX_EVENTS:
            self.events = [e for e in self.events[-MAX_EVENTS // 2:]]
        self._notify.set()
        self._notify = asyncio.Event()

    async def wait(self, after: int, timeout: float) -> None:
        if self.seq > after or self.status not in ACTIVE:
            return
        ev = self._notify
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "conversation_id": self.conversation_id,
                "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
                "profile": self.profile, "result": self.result, "error": self.error, "seq": self.seq}


class JobManager:
    def __init__(self, platform: Any):
        self.p = platform
        self.jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------ admission
    def _check_admission(self, user: dict, kind: str, cost: float) -> None:
        p = self.p
        if not p.governor.state.accept_jobs:
            raise AdmissionError("server_busy", "サーバーが高負荷のため、新しいリクエストを一時的に受け付けていません", 503)
        ok, retry = p.ratelimiter.hit(f"jobs:{user['id']}", user["rate_limit_per_min"] / 60.0,
                                      max(1, user["rate_limit_per_min"] // 3))
        if not ok:
            raise AdmissionError("rate_limited", f"リクエストが多すぎます。{int(retry) + 1}秒後に再試行してください")
        active = sum(1 for j in self.jobs.values() if j.user_id == user["id"] and j.status in ACTIVE)
        if active >= p.settings.scheduler.max_queued_per_user:
            raise AdmissionError("too_many_jobs", "実行中・待機中のリクエストが多すぎます。完了をお待ちください")
        if cost > 0:
            used = float(p.db.scalar("SELECT generation_units FROM usage_daily WHERE user_id=? AND day=?",
                                     (user["id"], day_key())) or 0)
            if used + cost > user["generation_quota_daily"]:
                raise AdmissionError("quota_exceeded",
                                     f"本日の生成クォータ ({user['generation_quota_daily']}) を超えます", 403)
            if p.governor.state.disk == "critical":
                raise AdmissionError("disk_full", "ディスク容量が不足しているため生成を停止しています", 507)

    def create(self, user: dict, kind: str, request: dict, conversation_id: str | None = None,
               cost: float = 0.0) -> Job:
        self._check_admission(user, kind, cost)
        job = Job(new_id(), user["id"], kind, conversation_id, request)
        self.p.db.execute("INSERT INTO jobs(id, user_id, kind, status, conversation_id, created_at, request)"
                          " VALUES (?,?,?,?,?,?,?)", (job.id, user["id"], kind, job.status, conversation_id,
                                                      job.created_at, dumps(request)))
        self.jobs[job.id] = job
        return job

    def start(self, job: Job, runner: Callable[[Job], Awaitable[dict | None]]) -> None:
        job.task = asyncio.get_running_loop().create_task(self._run(job, runner), name=f"job-{job.id[:8]}")

    async def _run(self, job: Job, runner: Callable[[Job], Awaitable[dict | None]]) -> None:
        job.status, job.started_at = "running", now()
        job.emit("status", status="running")
        self.p.db.execute("UPDATE jobs SET status=?, started_at=? WHERE id=?", ("running", job.started_at, job.id))
        timeout = self.p.settings.scheduler.job_timeout_seconds
        try:
            result = await asyncio.wait_for(runner(job), timeout)
            job.result = result or {}
            job.status = "done"
        except asyncio.CancelledError:
            job.status, job.error = "cancelled", "キャンセルされました"
        except asyncio.TimeoutError:
            job.status, job.error = "failed", "タスクの最大実行時間を超えました"
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", type(e).__name__)
            job.status, job.error = "failed", getattr(e, "message", None) or str(e) or type(e).__name__
            log.exception("job %s failed", job.id) if not hasattr(e, "code") else log.info("job %s: %s", job.id, e)
            job.emit("error", code=code, message=job.error)
        finally:
            job.cancel_event.set() if job.status == "cancelled" else None
            job.finished_at = now()
            job.emit("done", status=job.status, error=job.error, result=job.result)
            self._persist(job)
            asyncio.get_running_loop().call_later(900, self.jobs.pop, job.id, None)

    def begin_inline(self, user: dict, kind: str, request: dict) -> Job:
        """A job driven by the caller (e.g. an OpenAI-compatible request) instead of a background task."""
        job = self.create(user, kind, request)
        job.status, job.started_at = "running", now()
        self.p.db.execute("UPDATE jobs SET status=?, started_at=? WHERE id=?", ("running", job.started_at, job.id))
        return job

    def finish_inline(self, job: Job, status: str, error: str | None = None, result: dict | None = None) -> None:
        job.status, job.error, job.result, job.finished_at = status, error, result or {}, now()
        job.emit("done", status=status, error=error, result=job.result)
        self._persist(job)
        try:
            asyncio.get_running_loop().call_later(900, self.jobs.pop, job.id, None)
        except RuntimeError:
            self.jobs.pop(job.id, None)

    def _persist(self, job: Job) -> None:
        dur = (job.finished_at or now()) - (job.started_at or job.created_at)
        self.p.db.execute("UPDATE jobs SET status=?, finished_at=?, profile=?, result=?, error=?, gpu_seconds=? WHERE id=?",
                          (job.status, job.finished_at, dumps(job.profile), dumps(job.result), job.error,
                           job.gpu_seconds, job.id))
        self.record_usage(job.user_id, jobs=1, gpu_seconds=job.gpu_seconds)
        log.info("job %s %s kind=%s %.1fs", job.id[:8], job.status, job.kind, dur)

    def record_usage(self, user_id: str, *, jobs: int = 0, gpu_seconds: float = 0.0, units: float = 0.0,
                     tokens: int = 0) -> None:
        self.p.db.execute(
            "INSERT INTO usage_daily(user_id, day, generation_units, gpu_seconds, jobs, tokens) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id, day) DO UPDATE SET generation_units=generation_units+excluded.generation_units,"
            " gpu_seconds=gpu_seconds+excluded.gpu_seconds, jobs=jobs+excluded.jobs, tokens=tokens+excluded.tokens",
            (user_id, day_key(), units, gpu_seconds, jobs, tokens))

    # ------------------------------------------------------------------ access
    def get(self, job_id: str, user_id: str | None = None) -> Job | None:
        job = self.jobs.get(job_id)
        if job and (user_id is None or job.user_id == user_id):
            return job
        return None

    def get_record(self, job_id: str, user_id: str | None = None) -> dict | None:
        job = self.get(job_id, user_id)
        if job:
            return job.public()
        sql = "SELECT * FROM jobs WHERE id=?" + (" AND user_id=?" if user_id else "")
        row = self.p.db.one(sql, (job_id, user_id) if user_id else (job_id,))
        if not row:
            return None
        return {"id": row["id"], "kind": row["kind"], "status": row["status"], "conversation_id": row["conversation_id"],
                "created_at": row["created_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
                "profile": loads(row["profile"], {}), "result": loads(row["result"], {}), "error": row["error"], "seq": 0}

    def list_for_user(self, user_id: str, limit: int = 50) -> list[dict]:
        rows = self.p.db.query("SELECT id FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        return [r for r in (self.get_record(x["id"], user_id) for x in rows) if r]

    def cancel(self, job_id: str, user_id: str | None = None) -> bool:
        job = self.get(job_id, user_id)
        if not job or job.status not in ACTIVE:
            return False
        job.cancel_event.set()
        if job.task:
            job.task.cancel()
        return True

    async def cancel_all(self) -> None:
        for job in list(self.jobs.values()):
            if job.status in ACTIVE and job.task:
                job.cancel_event.set()
                job.task.cancel()
        await asyncio.sleep(0)

    def queue_info(self, job: Job) -> dict | None:
        if not job.current_unit:
            return None
        pos = self.p.scheduler.position(job.current_unit)
        if pos is None:
            return None
        return {"position": pos[0], "eta_seconds": pos[1]}

    async def stream(self, job: Job, after: int = 0) -> AsyncIterator[dict]:
        if job.events and after < job.events[0]["seq"] - 1:
            yield {"seq": job.events[0]["seq"] - 1, "type": "snapshot", "data": {"text": job.text}}
        while True:
            pending = [e for e in job.events if e["seq"] > after]
            for e in pending:
                yield e
                after = e["seq"]
            if job.status not in ACTIVE and not [e for e in job.events if e["seq"] > after]:
                return
            await job.wait(after, 15.0)
            if job.seq <= after and job.status in ACTIVE:
                yield {"seq": after, "type": "ping", "data": {}}
