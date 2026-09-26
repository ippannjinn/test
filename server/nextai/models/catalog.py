from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

KINDS = ("llm", "vlm", "embedding", "image", "video", "music")
LLM_KINDS = ("llm", "vlm")


@dataclass
class ComponentSource:
    repo: str
    patterns: list[str]
    exclude: list[str] = field(default_factory=list)
    revision: str = "main"
    mode: str = "first"  # "first": one file; "all": every pattern must resolve (snapshot)


@dataclass
class ModelSpec:
    id: str
    display_name: str
    kind: str
    backend: str
    roles: list[str]
    license: str
    storage_group: str
    size_gb: float
    components: dict[str, list[ComponentSource]]
    arch: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, float] = field(default_factory=dict)
    speed: float = 0.5
    ctx_max: int = 8192
    defaults: dict[str, Any] = field(default_factory=dict)
    reasoning_control: str = "none"
    vram_mb: int | None = None
    args: list[str] = field(default_factory=list)
    optional_args: list[str] = field(default_factory=list)
    ladder: dict[str, Any] = field(default_factory=dict)  # {"family": "general", "tier": 30}: rung in a size ladder
    custom: bool = False

    def cap(self, name: str) -> float:
        return float(self.capabilities.get(name, 0.0))

    @property
    def is_llm(self) -> bool:
        return self.kind in LLM_KINDS

    @property
    def moe(self) -> bool:
        return bool(self.arch.get("moe"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "display_name": self.display_name, "kind": self.kind, "backend": self.backend,
            "roles": self.roles, "license": self.license, "storage_group": self.storage_group,
            "size_gb": self.size_gb, "arch": self.arch, "capabilities": self.capabilities, "speed": self.speed,
            "ctx_max": self.ctx_max, "defaults": self.defaults, "reasoning_control": self.reasoning_control,
            "vram_mb": self.vram_mb, "custom": self.custom, "ladder": self.ladder,
            "components": {k: [s.__dict__ for s in v] for k, v in self.components.items()},
        }


def parse_spec(d: dict[str, Any], custom: bool = False) -> ModelSpec:
    kind = d["kind"]
    if kind not in KINDS:
        raise ValueError(f"unknown model kind: {kind}")
    comps = {}
    for name, sources in d.get("components", {}).items():
        comps[name] = [ComponentSource(repo=s["repo"], patterns=list(s["patterns"]), exclude=list(s.get("exclude", [])),
                                       revision=s.get("revision", "main"), mode=s.get("mode", "first"))
                       for s in sources]
    if not comps:
        raise ValueError("model spec needs at least one component")
    return ModelSpec(
        id=d["id"], display_name=d.get("display_name", d["id"]), kind=kind, backend=d["backend"],
        roles=list(d.get("roles", [])), license=d.get("license", "unknown"),
        storage_group=d.get("storage_group", "text" if kind in LLM_KINDS else kind),
        size_gb=float(d.get("size_gb", 0)), components=comps, arch=dict(d.get("arch", {})),
        capabilities={k: float(v) for k, v in d.get("capabilities", {}).items()}, speed=float(d.get("speed", 0.5)),
        ctx_max=int(d.get("ctx_max", 8192)), defaults=dict(d.get("defaults", {})),
        reasoning_control=d.get("reasoning_control", "none"), vram_mb=d.get("vram_mb"),
        args=list(d.get("args", [])), optional_args=list(d.get("optional_args", [])),
        ladder=dict(d.get("ladder", {})), custom=custom)


class Catalog:
    def __init__(self, data: dict[str, Any], custom_specs: list[dict[str, Any]] | None = None):
        self.raw = data
        self.models: dict[str, ModelSpec] = {}
        for d in data["models"]:
            spec = parse_spec(d)
            self.models[spec.id] = spec
        for d in custom_specs or []:
            spec = parse_spec(d, custom=True)
            self.models[spec.id] = spec
        self.sets: list[dict[str, Any]] = data.get("sets", [])
        self.runtimes: dict[str, dict[str, Any]] = data.get("runtimes", {})
        for s in self.sets:
            missing = [m for m in s["models"] if m not in self.models]
            if missing:
                raise ValueError(f"set {s['id']} references unknown models {missing}")

    @classmethod
    def load(cls, custom_specs: list[dict[str, Any]] | None = None) -> "Catalog":
        text = resources.files("nextai.catalog").joinpath("models.json").read_text(encoding="utf-8")
        return cls(json.loads(text), custom_specs)

    def get(self, model_id: str) -> ModelSpec:
        return self.models[model_id]

    def set_by_id(self, set_id: str) -> dict[str, Any]:
        for s in self.sets:
            if s["id"] == set_id:
                return s
        raise KeyError(set_id)

    def set_size_gb(self, s: dict[str, Any]) -> float:
        return round(sum(self.models[m].size_gb for m in s["models"])
                     + sum(self.runtimes.get(r, {}).get("size_gb", 0) for r in s.get("runtimes", [])), 1)

    def select_set(self, *, vram_gb: float, ram_gb: float, disk_free_gb: float) -> dict[str, Any]:
        """Pick the richest set this PC can run long-term (not simply the biggest models)."""
        evaluated = []
        for s in self.sets:
            req = s.get("requires", {})
            ok = (vram_gb >= req.get("vram_gb", 0) and ram_gb >= req.get("ram_gb", 0)
                  and disk_free_gb >= req.get("disk_free_gb", 0))
            evaluated.append({**s, "size_gb": self.set_size_gb(s), "eligible": ok})
        for s in evaluated:
            if s["eligible"] and s.get("auto", True):
                return {"selected": s["id"], "sets": evaluated}
        return {"selected": None, "sets": evaluated}
