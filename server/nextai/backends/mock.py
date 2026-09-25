"""Deterministic mock backends for tests, CI and demo mode (no GPU, no model files).

They exercise the full platform path (model manager, scheduler, VRAM accounting) by registering
simulated allocations with the MockGpuProvider.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import struct
import threading
import wave
from pathlib import Path
from typing import Any, AsyncIterator

from PIL import Image, ImageDraw

from ..models.catalog import ModelSpec
from ..models.planner import LaunchPlan
from ..resources.monitor import MockGpuProvider
from .base import BackendError, ChatEvent, ChatRequest, LLMBackend, MediaBackend, ProgressFn

_TOOL_DIRECTIVE = re.compile(r"\[\[tool:(\w+)\s*(\{.*?\})?\]\]", re.S)


class MockInstance:
    def __init__(self, spec: ModelSpec, plan: LaunchPlan):
        self.spec, self.plan, self.alive = spec, plan, True


class MockLLMBackend(LLMBackend):
    name = "mock"
    available = True

    def __init__(self, gpu: MockGpuProvider | None = None, load_delay: float = 0.02, token_delay: float = 0.0):
        self.gpu = gpu
        self.load_delay = load_delay
        self.token_delay = token_delay
        self.loads: list[str] = []
        self.fail_next_load = False

    async def start(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan) -> MockInstance:
        await asyncio.sleep(self.load_delay)
        if self.fail_next_load:
            self.fail_next_load = False
            raise BackendError("mock load failure")
        if self.gpu and plan.gpu:
            self.gpu.allocate(f"llm:{spec.id}", plan.est_vram_mb)
        self.loads.append(spec.id)
        return MockInstance(spec, plan)

    async def stop(self, instance: MockInstance) -> None:
        instance.alive = False
        if self.gpu:
            self.gpu.free(f"llm:{instance.spec.id}")

    def alive(self, instance: MockInstance) -> bool:
        return instance is not None and instance.alive

    async def chat(self, instance: MockInstance, spec: ModelSpec, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        if self.gpu:
            self.gpu.active += 1
        try:
            users = [_text_of(m.get("content")) for m in req.messages if m["role"] == "user"]
            text = next((t for t in reversed(users) if _TOOL_DIRECTIVE.search(t)), users[-1] if users else "")
            answered = {m.get("tool_call_id") for m in req.messages if m["role"] == "tool"}
            n_tool_msgs = sum(1 for m in req.messages if m["role"] == "tool")
            if req.tools:
                directives = _TOOL_DIRECTIVE.findall(text)
                if n_tool_msgs < len(directives):
                    name, args = directives[n_tool_msgs]
                    call_id = f"call_{n_tool_msgs}"
                    if call_id not in answered:
                        yield ChatEvent("tool_calls", tool_calls=[{"id": call_id, "name": name, "arguments": args or "{}"}])
                        yield ChatEvent("done", finish_reason="tool_calls", usage={"completion_tokens": 8})
                        return
            system = " ".join(_text_of(m.get("content")) for m in req.messages if m["role"] == "system")
            if "VERIFY" in system:
                reply = "PASS"
            elif "PLAN" in system and "計画" in system:
                reply = "1. 要件を整理する\n2. 実行する\n3. 検証する"
            elif req.json_mode:
                reply = json.dumps({"prompt": _TOOL_DIRECTIVE.sub("", text).strip()[:200] or "image"})
            else:
                tool_results = [_text_of(m.get("content")) for m in req.messages if m["role"] == "tool"]
                body = _TOOL_DIRECTIVE.sub("", text).strip()
                reply = f"[mock:{spec.id}] {body[:300]}"
                if tool_results:
                    reply += "\n\nツール結果: " + " | ".join(t[:120] for t in tool_results)
            if req.reasoning != "off":
                yield ChatEvent("reasoning", text="(mock reasoning) ")
            for i in range(0, len(reply), 12):
                if self.token_delay:
                    await asyncio.sleep(self.token_delay)
                yield ChatEvent("content", text=reply[i:i + 12])
            yield ChatEvent("done", finish_reason="stop",
                            usage={"prompt_tokens": sum(len(_text_of(m.get("content"))) for m in req.messages) // 3,
                                   "completion_tokens": len(reply) // 3},
                            timings={"predicted_per_second": 42.0, "prompt_per_second": 900.0})
        finally:
            if self.gpu:
                self.gpu.active = max(0, self.gpu.active - 1)

    async def embed(self, instance: MockInstance, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(t) for t in texts]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def hash_embedding(text: str, dims: int = 256) -> list[float]:
    """Character-trigram hashing vector: cheap, deterministic, works for Japanese."""
    vec = [0.0] * dims
    t = f"  {text.lower()}  "
    for i in range(len(t) - 2):
        h = int.from_bytes(hashlib.blake2b(t[i:i + 3].encode(), digest_size=4).digest(), "little")
        vec[h % dims] += 1.0 if (h >> 16) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class MockMediaBackend(MediaBackend):
    name = "mock"
    available = True
    kinds = ("image", "video", "music")

    def __init__(self, gpu: MockGpuProvider | None = None, step_delay: float = 0.01):
        self.gpu, self.step_delay = gpu, step_delay

    async def generate(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan | None,
                       params: dict[str, Any], out_dir: Path, progress: ProgressFn,
                       cancel: threading.Event) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        seed = int(hashlib.sha256(str(params.get("prompt", "")).encode()).hexdigest()[:8], 16)
        steps = 4
        for i in range(steps):
            if cancel.is_set():
                raise BackendError("キャンセルされました")
            await asyncio.sleep(self.step_delay)
            progress((i + 1) / steps, f"step {i + 1}/{steps}")
        if spec.kind == "image":
            img = _mock_image(int(params.get("width", 512)), int(params.get("height", 512)), seed, str(params.get("prompt", "")))
            p = out_dir / "out.png"
            img.save(p)
            return [p]
        if spec.kind == "video":
            frames = [_mock_image(int(params.get("width", 320)) // 2, int(params.get("height", 192)) // 2, seed + i * 7919, "")
                      for i in range(8)]
            p = out_dir / "video.webp"
            frames[0].save(p, format="WEBP", save_all=True, append_images=frames[1:], duration=120, loop=0)
            return [p]
        p = out_dir / "music.wav"
        _mock_wav(p, float(params.get("seconds", 2)), seed)
        return [p]


def _mock_image(w: int, h: int, seed: int, label: str) -> Image.Image:
    w, h = max(16, min(w, 1024)), max(16, min(h, 1024))
    c1 = ((seed >> 0) & 255, (seed >> 8) & 255, (seed >> 16) & 255)
    c2 = (255 - c1[0], 255 - c1[1], (c1[2] + 128) % 256)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    if label:
        d.text((8, 8), "MOCK: " + label[:40], fill=(255, 255, 255))
    return img


def _mock_wav(path: Path, seconds: float, seed: int, rate: int = 22050) -> None:
    notes = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25]
    n = int(max(0.5, min(seconds, 10)) * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            f = notes[(seed + i // (rate // 4)) % len(notes)]
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * f * i / rate)))
        w.writeframes(bytes(frames))
