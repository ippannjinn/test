"""VRAM/RAM placement planning.

Tiers for llama.cpp models:
  Hot  (VRAM): attention/dense weights, KV cache and as many MoE expert layers as the budget allows.
  Warm (RAM):  remaining MoE expert layers (``--n-cpu-moe``), served from the OS page cache.
  Cold (NVMe): the GGUF is memory-mapped, so rarely used experts stay on disk until touched.
The plan is recomputed on every (re)load from the live VRAM budget, so the split adapts to
what else (games, other models) is using the GPU.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .catalog import ModelSpec

KV_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}
MIN_CTX = 4096


@dataclass
class LaunchPlan:
    model_id: str
    gpu: bool
    n_gpu_layers: int
    n_cpu_moe: int
    ctx: int
    parallel: int
    kv_type: str
    est_vram_mb: int
    est_ram_mb: int
    offload: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def kv_mb(spec: ModelSpec, ctx: int, kv_type: str) -> float:
    a = spec.arch
    per_token = 2 * a.get("n_layers", 32) * a.get("n_kv_heads", 8) * a.get("head_dim", 128) * KV_BYTES.get(kv_type, 2.0)
    return per_token * ctx / 2**20


def overhead_mb(spec: ModelSpec, ctx: int) -> float:
    big = spec.arch.get("total_params_b", 7) >= 10
    return (520 if big else 380) + ctx / 256 + spec.arch.get("extra_vram_mb", 0)


def plan_llm(spec: ModelSpec, file_mb: float, *, vram_budget_mb: float, ram_budget_mb: float,
             ctx: int | None = None, parallel: int | None = None, kv_type: str = "q8_0",
             correction: float = 1.0, has_gpu: bool = True) -> LaunchPlan | None:
    ctx = int(min(ctx or spec.defaults.get("ctx", 8192), spec.ctx_max))
    parallel = int(parallel or spec.defaults.get("parallel", 1))
    n_layers = int(spec.arch.get("n_layers", 32))
    correction = max(0.7, min(1.6, correction or 1.0))

    if spec.defaults.get("cpu_only") or not has_gpu or vram_budget_mb <= 0:
        if file_mb > 6000 and not spec.defaults.get("cpu_only"):
            return None
        return LaunchPlan(spec.id, False, 0, 0, ctx, parallel, "f16", 0, int(file_mb + kv_mb(spec, ctx, "f16")),
                          notes=["CPU実行"])

    budget = vram_budget_mb / correction
    while True:
        kv, ov = kv_mb(spec, ctx, kv_type), overhead_mb(spec, ctx)
        if spec.moe:
            ef = float(spec.arch.get("expert_fraction", 0.9))
            expert_total, dense = file_mb * ef, file_mb * (1 - ef)
            per_layer = expert_total / n_layers
            base = dense + kv + ov
            if base <= budget:
                gpu_expert_layers = int(max(0, min(n_layers, (budget - base) // per_layer)))
                n_cpu_moe = n_layers - gpu_expert_layers
                vram = base + gpu_expert_layers * per_layer
                ram = n_cpu_moe * per_layer
                if ram <= ram_budget_mb + file_mb * 0.5:  # mmap: cold experts may stay on NVMe
                    notes = [f"MoE: expert {gpu_expert_layers}/{n_layers}層をVRAM, {n_cpu_moe}層をRAM/NVMe"]
                    return LaunchPlan(spec.id, True, 999, n_cpu_moe, ctx, parallel, kv_type,
                                      int(vram * correction), int(ram), notes=notes)
        else:
            full = file_mb + kv + ov
            if full <= budget:
                return LaunchPlan(spec.id, True, 999, 0, ctx, parallel, kv_type, int(full * correction), 0,
                                  notes=["全層VRAM"])
            per_layer = file_mb * 0.92 / n_layers
            avail = budget - kv - ov - file_mb * 0.08
            layers = int(avail // per_layer) if avail > 0 else 0
            if layers >= n_layers * 0.5:
                ram = (n_layers - layers) * per_layer
                if ram <= ram_budget_mb:
                    vram = file_mb * 0.08 + layers * per_layer + kv + ov
                    return LaunchPlan(spec.id, True, layers, 0, ctx, parallel, kv_type, int(vram * correction),
                                      int(ram), notes=[f"部分オフロード {layers}/{n_layers}層"])
        if ctx // 2 < MIN_CTX:
            break
        ctx //= 2
        parallel = max(1, min(parallel, ctx // 2048))
    return None


def plan_media(spec: ModelSpec, *, vram_budget_mb: float, correction: float = 1.0, has_gpu: bool = True) -> LaunchPlan | None:
    need = int((spec.vram_mb or 6000) * max(0.7, min(1.6, correction or 1.0)))
    if not has_gpu:
        return None
    if need <= vram_budget_mb:
        return LaunchPlan(spec.id, True, 0, 0, 0, 1, "", need, 0)
    if need * 0.6 <= vram_budget_mb:
        return LaunchPlan(spec.id, True, 0, 0, 0, 1, "", int(vram_budget_mb), need - int(vram_budget_mb),
                          offload=True, notes=["一部をRAMへオフロード"])
    return None
