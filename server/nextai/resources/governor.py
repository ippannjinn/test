"""Turns raw resource snapshots into a pressure level and concrete admission limits.

The host PC is a normal Windows desktop: when RAM/VRAM/temperature/disk approach danger, the
governor escalates step by step (stop prefetch → refuse heavy work → shed models → refuse new jobs).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum

from ..config import Settings
from .monitor import Snapshot


class Level(IntEnum):
    NORMAL = 0
    ELEVATED = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class GovernorState:
    level: Level = Level.NORMAL
    reasons: list[str] = field(default_factory=list)
    vram_total_mb: int = 0
    vram_budget_mb: int = 0
    ram_budget_mb: int = 0
    allow_prefetch: bool = True
    allow_heavy: bool = True
    accept_jobs: bool = True
    gpu_paused: bool = False
    llm_parallel_factor: float = 1.0
    sandbox_factor: float = 1.0
    disk: str = "ok"
    has_gpu: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.name
        return d


class Governor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = GovernorState()
        self._ram_level = Level.NORMAL
        self._improve_streak = 0
        self._cpu_high_streak = 0

    def _ram_level_for(self, avail: int, margin: int = 0) -> Level:
        r = self.settings.resources
        if avail < r.ram_critical_mb + margin:
            return Level.CRITICAL
        if avail < r.ram_high_mb + margin:
            return Level.HIGH
        if avail < r.ram_elevated_mb + margin:
            return Level.ELEVATED
        return Level.NORMAL

    def evaluate(self, snap: Snapshot) -> GovernorState:
        r = self.settings.resources
        reasons: list[str] = []

        # RAM with hysteresis: escalate immediately, relax only after 3 good samples beyond the margin.
        raw = self._ram_level_for(snap.ram_available_mb)
        if raw > self._ram_level:
            self._ram_level, self._improve_streak = raw, 0
        elif raw < self._ram_level:
            relaxed = self._ram_level_for(snap.ram_available_mb, r.ram_hysteresis_mb)
            if relaxed < self._ram_level:
                self._improve_streak += 1
                if self._improve_streak >= 3:
                    self._ram_level, self._improve_streak = relaxed, 0
            else:
                self._improve_streak = 0
        level = self._ram_level
        if level > Level.NORMAL:
            reasons.append(f"RAM残り {snap.ram_available_mb}MB")

        # CPU: sustained saturation only.
        self._cpu_high_streak = self._cpu_high_streak + 1 if snap.cpu_percent >= r.cpu_high_percent else 0
        if self._cpu_high_streak >= 5:
            level = max(level, Level.ELEVATED)
            reasons.append(f"CPU高負荷 {snap.cpu_percent:.0f}%")

        # GPU: budget excludes VRAM used by other programs (games, browser) when host_friendly.
        gpu = snap.gpu
        vram_total = gpu.vram_total_mb if gpu else 0
        gpu_paused = False
        if gpu:
            external = snap.external_vram_mb if r.host_friendly else 0
            budget = max(0, vram_total - r.vram_reserve_mb - external)
            if external > vram_total * 0.35:
                level = max(level, Level.ELEVATED)
                reasons.append(f"他アプリがVRAMを使用中 ({external}MB)")
            if gpu.temp_c is not None:
                if gpu.temp_c >= r.gpu_temp_pause_c:
                    level, gpu_paused = max(level, Level.HIGH), True
                    reasons.append(f"GPU温度 {gpu.temp_c:.0f}°C (一時停止)")
                elif gpu.temp_c >= r.gpu_temp_warn_c:
                    level = max(level, Level.ELEVATED)
                    reasons.append(f"GPU温度 {gpu.temp_c:.0f}°C")
        else:
            budget = 0

        # Disk: keep the safety margin free at all times.
        disk = "ok"
        if snap.disk_total_gb > 0:
            if snap.disk_free_gb < r.disk_margin_gb:
                disk = "critical"
                level = max(level, Level.HIGH)
                reasons.append(f"ディスク残り {snap.disk_free_gb:.1f}GB (安全マージン未満)")
            elif snap.disk_free_gb < r.disk_margin_gb + r.disk_low_buffer_gb:
                disk = "low"
                reasons.append(f"ディスク残り {snap.disk_free_gb:.1f}GB")

        ram_budget = max(0, snap.ram_available_mb - r.ram_elevated_mb)
        st = GovernorState(
            level=level, reasons=reasons, vram_total_mb=vram_total, vram_budget_mb=int(budget),
            ram_budget_mb=int(ram_budget),
            allow_prefetch=level == Level.NORMAL and self.settings.models.prefetch,
            allow_heavy=level <= Level.ELEVATED and not gpu_paused,
            accept_jobs=level < Level.CRITICAL,
            gpu_paused=gpu_paused,
            llm_parallel_factor={Level.NORMAL: 1.0, Level.ELEVATED: 1.0, Level.HIGH: 0.5, Level.CRITICAL: 0.25}[level],
            sandbox_factor={Level.NORMAL: 1.0, Level.ELEVATED: 0.5, Level.HIGH: 0.5, Level.CRITICAL: 0.0}[level],
            disk=disk, has_gpu=gpu is not None,
        )
        self.state = st
        return st
