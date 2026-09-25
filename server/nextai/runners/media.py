"""Image / video / music generation through the GPU scheduler."""
from __future__ import annotations

import json
import random
import re
import shutil
from typing import Any

from ..jobs import AdmissionError, Job
from ..models.manager import ModelUnavailable
from ..profile.engine import MEDIA_KIND, Profile
from ..services.storage import DiskFull
from .common import gpu_lease, llm_call

REFINE_PROMPT = ("You write prompts for a {kind} generation model. Rewrite the user's request as one concise, vivid "
                 "English prompt (max 60 words). Output JSON only: {{\"prompt\": \"...\"}}")
MIME = {".png": "image/png", ".webp": "image/webp", ".wav": "audio/wav", ".mp4": "video/mp4", ".webm": "video/webm"}


def clamp_params(p: Any, kind: str, params: dict) -> dict:
    g = p.settings.generation
    out = dict(params)
    if kind == "image":
        for k in ("width", "height"):
            out[k] = int(min(g.image_max_side, max(256, int(out.get(k, 768)))) // 64 * 64)
        out["steps"] = int(min(g.image_max_steps, max(1, int(out.get("steps", 4)))))
    elif kind == "video":
        for k in ("width", "height"):
            out[k] = int(min(g.video_max_side, max(160, int(out.get(k, 480)))) // 16 * 16)
        out["frames"] = int(min(g.video_max_frames, max(5, int(out.get("frames", 17)))))
        out["frames"] = (out["frames"] - 1) // 4 * 4 + 1
        out["steps"] = int(min(40, max(4, int(out.get("steps", 20)))))
    else:
        out["seconds"] = float(min(g.music_max_seconds, max(2, float(out.get("seconds", 10)))))
    out["seed"] = int(out.get("seed") or random.randint(1, 2**31 - 1))
    return out


async def refine_prompt(p: Any, job: Job, user: dict, profile: Profile, kind: str, prompt: str) -> str:
    llms = p.models.usable_models(("llm",))
    if not llms:
        return prompt
    fast = next((s for s in llms if "fast" in s.roles), llms[0])
    helper = Profile(task_type="writing", tuning=0.1, label="Speed", complexity=0.1, model_id=fast.id,
                     max_tokens=200, temperature=0.4, priority_class="interactive")
    try:
        res = await llm_call(p, job, user, helper, [{"role": "system", "content": REFINE_PROMPT.format(kind=kind)},
                                                    {"role": "user", "content": prompt}],
                             stream=False, json_mode=True, max_tokens=200)
        m = re.search(r"\{.*\}", res.content, re.S)
        refined = json.loads(m.group(0)).get("prompt", "") if m else ""
        refined = str(refined).strip()
        if 3 <= len(refined) <= 800:
            job.emit("tool_result", id="refine", name="prompt_refine", ok=True, summary=refined)
            return refined
    except (ModelUnavailable, ValueError):
        pass
    return prompt


async def run_media(p: Any, job: Job, user: dict, profile: Profile, prompt: str,
                    overrides: dict | None = None) -> list[dict]:
    kind = MEDIA_KIND[profile.task_type]
    if not profile.model_id:
        raise ModelUnavailable(f"{kind} 生成モデルがインストールされていません")
    spec = p.catalog.get(profile.model_id)
    cost = float(getattr(p.settings.generation, f"{kind}_cost"))
    try:
        p.storage.ensure_can_write(500 * 2**20, "生成")
    except DiskFull as e:
        raise AdmissionError("disk_full", str(e), 507) from e
    params = clamp_params(p, kind, {**profile.media, **(overrides or {})})
    prompt = re.sub(r"^[\s\-]+", "", prompt.strip())[:1500] or "a beautiful scene"
    params["prompt"] = prompt
    if profile.media.get("refine_prompt") and not (overrides or {}).get("raw_prompt"):
        params["prompt"] = await refine_prompt(p, job, user, profile, kind, prompt)
    params.setdefault("negative", "blurry, low quality, distorted")
    est = float(spec.defaults.get("seconds_estimate", 30))
    out_dir = p.settings.paths.tmp / f"gen-{job.id}"
    rows = []
    try:
        async with gpu_lease(p, job, user, profile, kind, spec.id, est):
            backend = p.models.backend_for(spec)
            paths = p.models.paths(spec.id) or {}
            plan = p.models.runtimes[spec.id].plan
            job.emit("progress", value=0.0, message="生成を開始します")
            outputs = await backend.generate(spec, paths, plan, params, out_dir,
                                             lambda v, m: job.emit("progress", value=round(v, 3), message=m),
                                             job.cancel_event)
        for i, f in enumerate(outputs[:4]):
            name = f"{kind}-{job.id[:8]}{'-' + str(i + 1) if len(outputs) > 1 else ''}{f.suffix}"
            row = p.files.save_path(user, f, name, kind="generated", job_id=job.id,
                                    meta={"prompt": prompt, "used_prompt": params["prompt"], "model": spec.id,
                                          "params": {k: v for k, v in params.items() if k not in ("prompt",)}})
            rows.append(row)
            job.emit("asset", file_id=row["id"], name=row["name"], mime=row["mime"], kind=kind)
        p.jobs.record_usage(user["id"], units=cost)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return rows
