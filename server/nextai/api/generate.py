from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth.deps import ApiError, Ctx, require_user
from ..runners.chat import run_generation

router = APIRouter(prefix="/api/generate", tags=["generate"])
User = Annotated[Ctx, Depends(require_user)]

NOTICES = {
    "image": "ローカルGPU (12GB) 向けの量子化モデルで生成します。",
    "video": "短尺・低解像度の動画のみ生成できます。クラウドの動画生成サービスとは品質・尺・速度が大きく異なります。"
             "生成には数分かかり、実行中は他の処理が待たされることがあります。",
    "music": "短いクリップのみ生成できます。モデルのライセンス上、非商用利用に限られます。",
}


class GenBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    params: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["auto", "fast", "quality"] = "auto"


@router.get("/capabilities")
def capabilities(ctx: User):
    p = ctx.p
    g = p.settings.generation
    out = {}
    for kind in ("image", "video", "music"):
        specs = p.models.usable_models((kind,))
        out[kind] = {
            "available": bool(specs), "models": [{"id": s.id, "name": s.display_name, "license": s.license} for s in specs],
            "notice": NOTICES[kind],
            "limits": {"image": {"max_side": g.image_max_side, "max_steps": g.image_max_steps},
                       "video": {"max_side": g.video_max_side, "max_frames": g.video_max_frames},
                       "music": {"max_seconds": g.music_max_seconds}}[kind],
            "defaults": specs[0].defaults if specs else {},
            "cost": getattr(g, f"{kind}_cost"),
        }
    return out


@router.post("/{kind}")
async def generate(kind: Literal["image", "video", "music"], body: GenBody, ctx: User):
    p = ctx.p
    if not p.models.usable_models((kind,)):
        raise ApiError(503, "unavailable", f"{kind} 生成モデルがインストールされていません")
    params = {k: v for k, v in body.params.items() if k in ("width", "height", "steps", "frames", "seconds", "seed", "negative")}
    if "negative" in params:
        params["negative"] = str(params["negative"])[:500]
    cost = float(getattr(p.settings.generation, f"{kind}_cost"))
    job = p.jobs.create(ctx.user, kind, {"kind": kind, "prompt": body.prompt, "params": params, "mode": body.mode}, cost=cost)
    p.jobs.start(job, lambda j: run_generation(p, j))
    return {"job": job.public()}
