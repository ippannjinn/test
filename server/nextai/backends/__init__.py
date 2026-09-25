from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..resources.monitor import GpuProvider, MockGpuProvider
from .base import LLMBackend, MediaBackend

log = logging.getLogger("nextai.backends")


@dataclass
class Backends:
    mode: str
    llm: LLMBackend
    image: MediaBackend
    video: MediaBackend
    music: MediaBackend

    def for_spec_backend(self, backend: str, kind: str):
        if kind in ("llm", "vlm", "embedding"):
            return self.llm
        return {"image": self.image, "video": self.video, "music": self.music}[kind]

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "llm": {"name": self.llm.name, "available": self.llm.available},
            "image": {"name": self.image.name, "available": self.image.available},
            "video": {"name": self.video.name, "available": self.video.available},
            "music": {"name": self.music.name, "available": self.music.available},
        }


def build_backends(settings: Settings, gpu: GpuProvider) -> Backends:
    mode = settings.models.backend_mode
    if mode == "mock":
        from .mock import MockLLMBackend, MockMediaBackend

        mg = gpu if isinstance(gpu, MockGpuProvider) else None
        media = MockMediaBackend(mg)
        return Backends("mock", MockLLMBackend(mg), media, media, media)
    from .llamacpp import LlamaCppBackend
    from .media import MusicGenBackend, SdCppBackend

    rt, logs = settings.paths.runtime, settings.paths.logs
    llm = LlamaCppBackend(settings, rt, logs)
    sd = SdCppBackend(settings, rt, logs)
    music = MusicGenBackend(settings, rt, logs)
    if not llm.available:
        log.warning("llama.cpp runtime not found under %s — text models unavailable until installed", rt)
    return Backends("real", llm, sd, sd, music)
