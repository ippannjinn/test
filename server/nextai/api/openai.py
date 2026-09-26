"""OpenAI-compatible API (/v1): chat completions (stream + tool calls), embeddings, models.

Requests use per-member API keys (scope "openai") and go through the same Dynamic Profile engine,
GPU scheduler, rate limits and concurrency caps as the web UI. model="auto" lets the platform pick.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..auth.deps import ApiError, Ctx, _resolve
from ..backends.base import BackendError, ChatRequest
from ..models.catalog import LLM_KINDS
from ..models.manager import HOT, ModelUnavailable
from ..profile.analyzer import analyze
from ..profile.engine import Profile
from ..runners.common import gpu_lease
from ..util import estimate_tokens

router = APIRouter(prefix="/v1", tags=["openai"])
AUTO = ("auto", "nextai-auto", "")
ROLES = ("system", "user", "assistant", "tool")


def require_api_key(request: Request) -> Ctx:
    p = request.app.state.platform
    if not p.settings.api.enabled:
        raise ApiError(403, "api_disabled", "API は管理者により無効化されています")
    ctx = _resolve(request)
    if ctx is None or ctx.auth != "token":
        raise ApiError(401, "invalid_api_key", "APIキーが無効です (Authorization: Bearer nxt_...)")
    if "openai" not in ctx.scopes:
        raise ApiError(403, "token_scope", "このキーには /v1 API の権限がありません")
    if ctx.user["must_change_password"]:
        raise ApiError(403, "password_change_required", "パスワードの変更が必要です。ブラウザでログインしてください")
    a = p.settings.auth
    ok, retry = p.ratelimiter.hit(f"api:{ctx.uid}", a.api_rate_per_second, a.api_burst)
    if not ok:
        raise ApiError(429, "rate_limited", "リクエストが多すぎます", {"Retry-After": str(int(retry) + 1)})
    return ctx


Key = Annotated[Ctx, Depends(require_api_key)]


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = "auto"
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=128)
    stop: str | list[str] | None = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    reasoning_effort: str | None = None


class EmbedBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input: str | list[str]
    model: str = "auto"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return ""


def _clean_messages(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in raw:
        role = m.get("role")
        if role == "developer":
            role = "system"
        if role not in ROLES:
            raise ApiError(400, "invalid_request", f"未対応の role です: {role}")
        msg: dict[str, Any] = {"role": role, "content": m.get("content") if m.get("content") is not None else ""}
        if not isinstance(msg["content"], (str, list)):
            raise ApiError(400, "invalid_request", "content は文字列または配列で指定してください")
        for k in ("name", "tool_call_id", "tool_calls"):
            if m.get(k) is not None:
                msg[k] = m[k]
        out.append(msg)
    return out


def _profile(p: Any, body: ChatBody, msgs: list[dict[str, Any]]) -> Profile:
    last = next((_text(m["content"]) for m in reversed(msgs) if m["role"] == "user"), "")
    a = analyze(last, history_turns=len(msgs) // 2)
    if a.is_media:
        a.task_type = "chat"
    elif a.task_type == "project":
        a.task_type = "coding"
    a.needs_web = a.needs_code_exec = a.needs_files = a.memory_op = False
    a.needs_vision = any(isinstance(m["content"], list) and any(isinstance(x, dict) and x.get("type") == "image_url"
                                                              for x in m["content"]) for m in msgs)
    prof = p.profiles.decide(a)
    if body.model not in AUTO:
        spec = p.catalog.models.get(body.model)
        if not spec or spec.kind not in LLM_KINDS or not p.models.usable(spec.id):
            raise ApiError(404, "model_not_found", f"モデル {body.model} は利用できません (GET /v1/models で確認)")
        prof.model_id, prof.model_name = spec.id, spec.display_name
        prof.reasoning = "medium" if spec.reasoning_control == "effort" else "off"
    if not prof.model_id:
        raise ApiError(503, "model_unavailable", "利用可能な言語モデルがありません")
    if body.reasoning_effort in ("low", "medium", "high"):
        prof.reasoning = body.reasoning_effort
    prof.use_agent, prof.tools = False, []
    prof.max_tokens = min(p.settings.api.max_tokens_cap, body.max_completion_tokens or body.max_tokens or prof.max_tokens)
    if body.temperature is not None:
        prof.temperature = body.temperature
    return prof


async def _produce(p: Any, ctx: Ctx, job: Any, prof: Profile, req: ChatRequest, out: asyncio.Queue) -> None:
    """Runs the scheduled inference and pushes ("event", payload) items; always ends with ("end", status)."""
    status, error = "done", None
    usage: dict[str, Any] = {}
    try:
        spec = p.catalog.get(prof.model_id)
        await out.put(("profile", prof))
        async with gpu_lease(p, job, ctx.user, prof, spec.kind, spec.id, max(4.0, req.max_tokens / 40.0)):
            rt = p.models.runtimes[spec.id]
            if rt.state != HOT or rt.instance is None:
                raise ModelUnavailable(f"{spec.display_name} が利用できません")
            await out.put(("started", None))
            async for ev in p.models.backend_for(spec).chat(rt.instance, spec, req):
                if ev.type == "done":
                    usage = ev.usage or {}
                await out.put(("chat", ev))
    except asyncio.CancelledError:
        status, error = "cancelled", "client disconnected"
        raise
    except (ModelUnavailable, BackendError) as e:
        status, error = "failed", str(e)
        await out.put(("error", str(e)))
    except Exception as e:  # noqa: BLE001
        status, error = "failed", f"{type(e).__name__}: {e}"
        await out.put(("error", error))
    finally:
        tokens = int(usage.get("prompt_tokens", 0) or 0) + int(usage.get("completion_tokens", 0) or 0)
        if tokens:
            p.jobs.record_usage(ctx.uid, tokens=tokens)
        p.jobs.finish_inline(job, status, error, {"model": prof.model_id, "usage": usage})
        out.put_nowait(("end", status))


def _chunk(cid: str, created: int, model: str, delta: dict | None, finish: str | None = None, usage: dict | None = None) -> str:
    obj: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _usage(u: dict[str, Any], prompt_est: int, completion_est: int) -> dict[str, int]:
    pt = int(u.get("prompt_tokens") or prompt_est)
    ct = int(u.get("completion_tokens") or completion_est)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def _tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"index": i, "id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"}}
            for i, c in enumerate(calls)]


@router.get("/models")
def list_models(ctx: Key):
    p = ctx.p
    data = [{"id": "auto", "object": "model", "created": 0, "owned_by": "nextai",
             "description": "Dynamic Profile が内容・混雑状況から最適なモデルを自動選択"}]
    for s in p.models.usable_models(("llm", "vlm", "embedding")):
        data.append({"id": s.id, "object": "model", "created": 0, "owned_by": "nextai", "description": s.display_name,
                     "kind": s.kind, "context_length": int(s.defaults.get("ctx", s.ctx_max))})
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(body: ChatBody, request: Request, ctx: Key):
    p = ctx.p
    msgs = _clean_messages(body.messages)
    prof = _profile(p, body, msgs)
    stops = [body.stop] if isinstance(body.stop, str) else body.stop
    req = ChatRequest(messages=msgs, max_tokens=prof.max_tokens, temperature=prof.temperature,
                      top_p=body.top_p if body.top_p is not None else 0.9, tools=body.tools or None,
                      reasoning=prof.reasoning, stop=stops,
                      json_mode=bool(body.response_format and body.response_format.get("type") == "json_object"))
    job = p.jobs.begin_inline(ctx.user, "api", {"model": body.model, "stream": body.stream, "messages": len(msgs)})
    job.profile = prof.to_dict()
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.get_running_loop().create_task(_produce(p, ctx, job, prof, req, queue))
    job.task = task
    cid, created, model = f"chatcmpl-{job.id}", int(time.time()), prof.model_id
    prompt_est = sum(estimate_tokens(_text(m["content"])) for m in msgs)

    if not body.stream:
        content, reasoning, calls, finish, usage, err = "", "", [], "stop", {}, None
        while True:
            kind, val = await queue.get()
            if kind == "chat":
                if val.type == "content":
                    content += val.text
                elif val.type == "reasoning":
                    reasoning += val.text
                elif val.type == "tool_calls":
                    calls = val.tool_calls
                elif val.type == "done":
                    finish, usage = val.finish_reason or "stop", val.usage
            elif kind == "error":
                err = val
            elif kind == "end":
                break
        if err:
            raise ApiError(503, "inference_failed", err)
        message: dict[str, Any] = {"role": "assistant", "content": content or (None if calls else "")}
        if reasoning:
            message["reasoning_content"] = reasoning
        if calls:
            message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in _tool_calls(calls)]
            finish = "tool_calls"
        return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": _usage(usage, prompt_est, estimate_tokens(content)),
                "nextai": {"profile": prof.label, "tuning": round(prof.tuning, 2), "model_name": prof.model_name}}

    include_usage = bool(body.stream_options and body.stream_options.get("include_usage"))

    async def stream():
        completion = ""
        try:
            yield _chunk(cid, created, model, {"role": "assistant", "content": ""})
            while True:
                try:
                    kind, val = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    pos = p.jobs.queue_info(job)
                    yield f": waiting{' position=' + str(pos['position']) if pos else ''}\n\n"
                    continue
                if await request.is_disconnected():
                    task.cancel()
                    return
                if kind == "chat":
                    if val.type == "content":
                        completion += val.text
                        yield _chunk(cid, created, model, {"content": val.text})
                    elif val.type == "reasoning":
                        yield _chunk(cid, created, model, {"reasoning_content": val.text})
                    elif val.type == "tool_calls":
                        yield _chunk(cid, created, model, {"tool_calls": _tool_calls(val.tool_calls)})
                    elif val.type == "done":
                        fr = "tool_calls" if val.finish_reason == "tool_calls" else (val.finish_reason or "stop")
                        yield _chunk(cid, created, model, {}, fr)
                        if include_usage:
                            yield _chunk(cid, created, model, None, usage=_usage(val.usage, prompt_est, estimate_tokens(completion)))
                elif kind == "error":
                    yield "data: " + json.dumps({"error": {"message": val, "type": "server_error", "code": "inference_failed"}},
                                                ensure_ascii=False) + "\n\n"
                elif kind == "end":
                    yield "data: [DONE]\n\n"
                    return
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/embeddings")
async def embeddings(body: EmbedBody, ctx: Key):
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > 256 or any(len(t) > 32000 for t in texts):
        raise ApiError(400, "invalid_request", "input は1〜256件、各32000文字以内で指定してください")
    res = await ctx.p.embed(texts)
    if not res:
        raise ApiError(503, "model_unavailable", "埋め込みモデルが利用できません")
    model_id, vecs = res
    n = sum(estimate_tokens(t) for t in texts)
    ctx.p.jobs.record_usage(ctx.uid, tokens=n)
    return {"object": "list", "model": model_id, "usage": {"prompt_tokens": n, "total_tokens": n},
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vecs)]}
