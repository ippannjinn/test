"""Dynamic Profile Engine.

Every request passes through here. Speed / Balanced / Autonomous are *tuning policies*, not
fixed profiles: a continuous tuning value t ∈ [0, 1] interpolates between them, and is shifted by
queue congestion, resource pressure and explicit user intent. The resulting profile decides the
model, reasoning depth, context, token/time/step budgets, tools, parallelism and queue class, and
can be re-evaluated mid-task (escalate on repeated failure, lighten under pressure, add tools).
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Settings
from ..models.catalog import ModelSpec
from ..models.manager import HOT, WARM, ModelManager
from ..resources.governor import Governor, Level
from ..util import clamp, lerp
from .analyzer import TaskAnalysis

POLICY_KEYS = ("max_tokens", "ctx_tokens", "max_steps", "max_seconds", "max_tool_calls", "tool_parallelism", "temperature")
MEDIA_KIND = {"image_gen": "image", "video_gen": "video", "music_gen": "music"}


def tuning_label(t: float) -> str:
    if t < 0.2:
        return "Speed"
    if t < 0.4:
        return "Speed寄りBalanced"
    if t < 0.6:
        return "Balanced"
    if t < 0.8:
        return "Balanced寄りAutonomous"
    return "Autonomous"


@dataclass
class Profile:
    task_type: str
    tuning: float
    label: str
    complexity: float
    model_id: str | None
    model_name: str = ""
    fallback_models: list[str] = field(default_factory=list)
    reasoning: str = "off"
    ctx_tokens: int = 4096
    max_tokens: int = 1024
    temperature: float = 0.7
    tools: list[str] = field(default_factory=list)
    tool_parallelism: int = 1
    use_agent: bool = False
    plan: bool = False
    verify: bool = False
    limits: dict[str, Any] = field(default_factory=dict)
    priority_class: str = "interactive"
    quality_pinned: bool = False
    wait_for_quality: bool = False
    congestion: float = 0.0
    pressure: str = "NORMAL"
    residency: str = "keep"
    media: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        return {"label": self.label, "tuning": round(self.tuning, 2), "task_type": self.task_type,
                "model_id": self.model_id, "model_name": self.model_name, "reasoning": self.reasoning,
                "tools": self.tools, "use_agent": self.use_agent, "priority_class": self.priority_class,
                "quality_pinned": self.quality_pinned, "wait_for_quality": self.wait_for_quality,
                "max_steps": self.limits.get("max_steps"), "revision": self.revision, "reasons": self.reasons[-6:]}


class ProfileEngine:
    def __init__(self, settings: Settings, manager: ModelManager, governor: Governor, congestion_fn=lambda: 0.0,
                 web_enabled_fn=lambda: True, sandbox_enabled_fn=lambda: True):
        self.settings, self.manager, self.governor = settings, manager, governor
        self.congestion_fn = congestion_fn
        self.web_enabled_fn = web_enabled_fn
        self.sandbox_enabled_fn = sandbox_enabled_fn

    # ------------------------------------------------------------------ policy interpolation
    def policy_at(self, t: float) -> dict[str, Any]:
        p = self.settings.get("profile")
        lo, hi, u = (p["speed"], p["balanced"], t / 0.5) if t <= 0.5 else (p["balanced"], p["autonomous"], (t - 0.5) / 0.5)
        out = {}
        for k in POLICY_KEYS:
            v = lerp(float(lo[k]), float(hi[k]), u)
            out[k] = round(v, 2) if k == "temperature" else int(round(v))
        return out

    # ------------------------------------------------------------------ decision
    def decide(self, a: TaskAnalysis) -> Profile:
        p = self.settings.profile
        reasons = list(a.reasons)
        t = 0.1 + a.complexity * 0.9
        pinned = a.explicit_mode == "quality"
        if a.explicit_mode == "fast":
            t = min(t, 0.2)
            reasons.append("速度優先の指定")
        if pinned:
            t = max(t, 0.75)
            reasons.append("品質優先の指定 (品質を落とさず待機)")
        floors = {"project": 0.78, "research": 0.45, "coding": 0.35, "file_analysis": 0.4, "reasoning": 0.45}
        if a.explicit_mode != "fast" and a.task_type in floors:
            t = max(t, floors[a.task_type])
        congestion = float(self.congestion_fn())
        gs = self.governor.state
        if not pinned:
            if congestion > 0.05:
                t -= p.congestion_shift * congestion
                reasons.append(f"混雑度 {congestion:.2f} → 軽量寄りに調整")
            if gs.level >= Level.HIGH:
                t -= 0.2
                reasons.append(f"リソース逼迫 ({gs.level.name}) → 軽量化")
            elif congestion < 0.05 and gs.level == Level.NORMAL and a.complexity > 0.5:
                t += p.idle_boost
                reasons.append("GPUに余裕あり → 高性能寄りに調整")
        t = clamp(t, 0.0, 1.0)
        prof = Profile(task_type=a.task_type, tuning=round(t, 3), label=tuning_label(t), complexity=a.complexity,
                       model_id=None, quality_pinned=pinned, congestion=round(congestion, 3), pressure=gs.level.name,
                       reasons=reasons)
        self._apply_policy(prof, a)
        self._choose_model(prof, a)
        if pinned and congestion > 0.3:
            prof.wait_for_quality = True
            prof.reasons.append("混雑中ですが品質指定のため本来の設定で順番待ちします")
        return prof

    def decide_media(self, a: TaskAnalysis) -> Profile:
        """Profile for one media generation (called by the generate_* tools and the /api/generate endpoint):
        same tuning as decide(), but picks the image/video/music model and its parameters."""
        chat = self.decide(a)
        prof = Profile(task_type=a.task_type, tuning=chat.tuning, label=chat.label, complexity=a.complexity, model_id=None,
                       quality_pinned=chat.quality_pinned, congestion=chat.congestion, pressure=chat.pressure,
                       reasons=[r for r in chat.reasons if "ロード予定" not in r])
        return self._media_profile(prof, a)

    def enable_workspace(self, prof: Profile) -> None:
        """The conversation already has sandbox files: keep them reachable (follow-ups like "そのファイルを渡して")."""
        if not self.sandbox_enabled_fn():
            return
        prof.tools = list(dict.fromkeys(prof.tools + ["run_code", "list_workspace", "read_workspace", "write_file", "share_file"]))
        if not prof.use_agent:
            prof.use_agent = True
            prof.limits = {**prof.limits, "max_steps": max(4, int(prof.limits.get("max_steps", 1))),
                           "max_tool_calls": max(4, int(prof.limits.get("max_tool_calls", 0) or 0))}

    def media_tools(self) -> list[str]:
        return [f"generate_{k}" for k in ("image", "video", "music") if self.manager.usable_models((k,))]

    def _apply_policy(self, prof: Profile, a: TaskAnalysis) -> None:
        t, p = prof.tuning, self.settings.profile
        pol = self.policy_at(t)
        prof.ctx_tokens = max(pol["ctx_tokens"], min(32768, a.input_tokens * 2 + 2048))
        prof.max_tokens = pol["max_tokens"]
        prof.temperature = pol["temperature"] if a.task_type not in ("coding", "project") else min(pol["temperature"], 0.4)
        prof.tool_parallelism = pol["tool_parallelism"]
        tools: list[str] = []
        web = self.web_enabled_fn()
        if web and (a.needs_web or (a.complexity > 0.05 and a.task_type not in ("translation",) and not a.is_media)):
            # like the cloud assistants: search is always at hand; the model decides when it needs it
            tools += ["web_research", "web_search", "web_fetch"]
        sandbox = self.sandbox_enabled_fn()
        if sandbox and (a.needs_code_exec or (a.task_type in ("coding", "reasoning") and t >= 0.6)):
            tools.append("run_code")
        if a.needs_files:
            tools.append("read_file")
        if a.task_type == "project":
            tools += ["write_file", "read_workspace", "list_workspace"] + (["run_code"] if sandbox else [])
        if t >= 0.4:
            tools.append("memory_search")
        if a.memory_op or t >= 0.6:
            tools.append("memory_save")
        if sandbox and ("run_code" in tools or a.needs_files or a.needs_web):
            # the per-conversation sandbox workspace: uploads, downloaded data and code outputs live there
            tools += ["list_workspace", "read_workspace", "write_file", "share_file"]
            if a.needs_web and self.web_enabled_fn():
                tools.append("download_file")
        media = self.media_tools()
        if a.is_media:
            # Media requests go through the LLM, which calls the generate_* tool with a prompt and parameters.
            wanted = f"generate_{MEDIA_KIND[a.task_type]}"
            tools = ([wanted] if wanted in media else []) + [m for m in media if m != wanted] + tools
            if wanted not in media:
                prof.reasons.append(f"{MEDIA_KIND[a.task_type]} 生成モデルが利用できません")
        elif any(not x.startswith("memory_") for x in tools):
            tools += media  # agent turns may also illustrate / score what they produce
        prof.tools = list(dict.fromkeys(tools))
        substantive = [x for x in prof.tools if not x.startswith("memory_")]
        prof.use_agent = bool(substantive) or (bool(prof.tools) and t >= 0.6)
        prof.plan = prof.use_agent and not a.is_media and (t >= p.autonomous_above or a.task_type == "project" or a.multi_step and t >= 0.5)
        prof.verify = prof.use_agent and not a.is_media and (t >= p.verify_above or a.task_type == "project")
        steps = pol["max_steps"] if prof.use_agent else 1
        max_seconds = pol["max_seconds"]
        if any(x.startswith("generate_") for x in prof.tools):
            g = self.settings.generation
            max_seconds = max(max_seconds, g.video_timeout_seconds if "generate_video" in prof.tools else 900)
        if a.deep_research:
            prof.plan = prof.verify = True
            steps = max(steps, 14)
            max_seconds = max(max_seconds, 1200)
        prof.limits = {"max_steps": max(steps, 3) if a.is_media else steps, "max_seconds": max_seconds,
                       "max_tool_calls": max(pol["max_tool_calls"], 2) if a.is_media else
                       max(pol["max_tool_calls"], 30) if a.deep_research else pol["max_tool_calls"],
                       "max_consecutive_failures": p.max_consecutive_failures, "max_total_tokens": p.max_total_tokens}
        prof.priority_class = "interactive" if t < 0.4 else "standard" if t < 0.75 else "batch"
        if prof.quality_pinned and prof.priority_class == "batch":
            prof.priority_class = "standard"

    def _score_model(self, spec: ModelSpec, prof: Profile, a: TaskAnalysis) -> float:
        t = prof.tuning
        cap = spec.cap(a.capability)
        quality = cap * 0.7 + spec.cap("japanese") * 0.3 if a.language == "ja" else cap
        rt = self.manager.runtimes[spec.id]
        hot = 1.0 if rt.state == HOT else 0.4 if rt.state == WARM else 0.0
        if prof.quality_pinned:
            return quality + 0.05 * hot
        w_q, w_s = 0.3 + 0.7 * t, 0.7 * (1 - t)
        w_h = 0.35 * (1 - t) + 0.3 * prof.congestion
        s = w_q * quality + w_s * spec.speed + w_h * hot
        if a.task_type in ("coding", "project") and "coding" in spec.roles:
            s += 0.1
        if "fast" in spec.roles and t < 0.3:
            s += 0.1
        if "reasoning" in spec.roles and t > 0.7 and a.task_type in ("reasoning", "coding", "project", "research"):
            s += 0.08
        return s

    def _choose_model(self, prof: Profile, a: TaskAnalysis) -> None:
        kinds: tuple[str, ...] = ("vlm",) if a.needs_vision else ("llm",)
        cands = self.manager.usable_models(kinds)
        if not cands and a.needs_vision:
            cands = self.manager.usable_models(("llm",))
            prof.reasons.append("画像理解モデルが無いため画像は解析できません")
        if not cands:
            prof.reasons.append("利用可能なモデルがありません")
            return
        ranked = sorted(cands, key=lambda s: self._score_model(s, prof, a), reverse=True)
        best = ranked[0]
        prof.model_id, prof.model_name = best.id, best.display_name
        prof.fallback_models = [s.id for s in ranked[1:]]
        rc = best.reasoning_control
        if rc == "effort":
            prof.reasoning = "low" if prof.tuning < 0.4 else "medium" if prof.tuning < 0.75 else "high"
        elif rc == "qwen3_toggle":
            prof.reasoning = "high" if prof.tuning >= self.settings.profile.reasoning_above else "off"
        else:
            prof.reasoning = "off"
        prof.ctx_tokens = min(prof.ctx_tokens, int(best.defaults.get("ctx", best.ctx_max)))
        if self.manager.runtimes[best.id].state != HOT:
            prof.reasons.append(f"{best.display_name} をロード予定 (スワップ)")

    def _media_profile(self, prof: Profile, a: TaskAnalysis) -> Profile:
        kind = MEDIA_KIND[a.task_type]
        g = self.settings.generation
        cands = self.manager.usable_models((kind,))
        prof.priority_class = "standard" if kind == "image" else "batch"
        prof.residency = "transient"
        prof.limits = {"max_steps": 1, "max_seconds": g.video_timeout_seconds if kind == "video" else 900}
        if not cands:
            prof.reasons.append(f"{kind} 生成モデルが利用できません")
            return prof
        spec = max(cands, key=lambda s: s.cap(kind))
        prof.model_id, prof.model_name = spec.id, spec.display_name
        t = prof.tuning
        d = spec.defaults
        if kind == "image":
            side = min(g.image_max_side, 768 if t < 0.3 else int(d.get("width", 1024)))
            prof.media = {"width": side, "height": side, "steps": min(g.image_max_steps, int(d.get("steps", 4)))}
        elif kind == "video":
            frames = min(g.video_max_frames, 17 if t < 0.3 else int(d.get("frames", 33)) if t < 0.75 else 49)
            w = min(g.video_max_side, 480 if t < 0.3 else int(d.get("width", 832)))
            h = int(w * int(d.get("height", 480)) / int(d.get("width", 832))) // 16 * 16
            prof.media = {"width": w, "height": h, "frames": frames, "steps": int(d.get("steps", 20)),
                          "fps": int(d.get("fps", 16))}
        else:
            secs = min(g.music_max_seconds, 8 if t < 0.3 else 15 if t < 0.75 else 30)
            prof.media = {"seconds": secs}
        llms = self.manager.usable_models(("llm",))
        prof.media["refine_prompt"] = bool(llms) and t >= 0.25 and kind != "music" or (bool(llms) and a.language == "ja")
        return prof

    # ------------------------------------------------------------------ mid-task re-evaluation
    def reevaluate(self, prof: Profile, signal: str, **info: Any) -> Profile | None:
        new = copy.deepcopy(prof)
        changed = False
        if signal == "consecutive_failures":
            better = [m for m in prof.fallback_models if self.manager.usable(m)]
            cur = self.manager.catalog.models.get(prof.model_id or "")
            if cur and better:
                cap = "coding" if prof.task_type in ("coding", "project") else "reasoning"
                stronger = [m for m in better if self.manager.catalog.get(m).cap(cap) > cur.cap(cap)]
                if stronger:
                    target = max(stronger, key=lambda m: self.manager.catalog.get(m).cap(cap))
                    new.fallback_models = [m for m in prof.fallback_models if m != target] + [prof.model_id]
                    new.model_id, new.model_name = target, self.manager.catalog.get(target).display_name
                    new.reasons.append(f"連続失敗のため {new.model_name} へ切替")
                    changed = True
            if not changed and not prof.plan:
                new.plan = True
                new.reasons.append("連続失敗のため計画から立て直し")
                changed = True
        elif signal == "repetition":
            new.temperature = min(1.0, prof.temperature + 0.2)
            new.plan = True
            new.reasons.append("同一操作の繰り返しを検出 → 戦略変更")
            changed = True
        elif signal == "pressure":
            if self.governor.state.level >= Level.HIGH:
                new.tool_parallelism = 1
                new.max_tokens = max(512, int(prof.max_tokens * 0.7))
                # lighter, but never so short that a research / tool task can't finish
                keep = 6 if any(t.startswith("web_") for t in prof.tools) else 4
                new.limits["max_steps"] = max(1, min(prof.limits.get("max_steps", 1), max(keep, prof.limits.get("max_steps", 1) // 2)))
                new.tuning = max(0.0, prof.tuning - 0.2)
                new.label = tuning_label(new.tuning)
                new.reasons.append("リソース逼迫のため途中で軽量化")
                changed = True
        elif signal == "needs_web":
            if "web_search" not in prof.tools and self.web_enabled_fn():
                new.tools = prof.tools + ["web_research", "web_search", "web_fetch"]
                new.use_agent = True
                new.limits["max_steps"] = max(prof.limits.get("max_steps", 1), 3)
                new.reasons.append("情報不足のためWeb検索を追加")
                changed = True
        elif signal == "model_unavailable":
            alts = [m for m in prof.fallback_models if self.manager.usable(m) and m != info.get("model_id")]
            if alts:
                new.model_id = alts[0]
                new.model_name = self.manager.catalog.get(alts[0]).display_name
                new.fallback_models = alts[1:]
                new.reasons.append(f"モデルを読み込めないため {new.model_name} へ切替")
                changed = True
        if not changed:
            return None
        new.revision = prof.revision + 1
        return new
