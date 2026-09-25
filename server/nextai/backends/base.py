from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ..models.catalog import ModelSpec
from ..models.planner import LaunchPlan


class BackendError(RuntimeError):
    pass


@dataclass
class ChatRequest:
    messages: list[dict[str, Any]]
    max_tokens: int = 1024
    temperature: float = 0.7
    top_p: float = 0.9
    tools: list[dict[str, Any]] | None = None
    reasoning: str = "off"  # off|low|medium|high
    stop: list[str] | None = None
    json_mode: bool = False


@dataclass
class ChatEvent:
    type: str  # content | reasoning | tool_calls | done
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)


ProgressFn = Callable[[float, str], None]


class LLMBackend:
    name = "none"
    available = False

    async def start(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan) -> Any:
        raise BackendError("LLM backend unavailable")

    async def stop(self, instance: Any) -> None:
        pass

    def alive(self, instance: Any) -> bool:
        return instance is not None

    def chat(self, instance: Any, spec: ModelSpec, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        raise BackendError("LLM backend unavailable")

    async def embed(self, instance: Any, texts: list[str]) -> list[list[float]]:
        raise BackendError("LLM backend unavailable")


class MediaBackend:
    name = "none"
    available = False
    kinds: tuple[str, ...] = ()

    async def generate(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan | None,
                       params: dict[str, Any], out_dir: Path, progress: ProgressFn,
                       cancel: Any) -> list[Path]:
        raise BackendError("media backend unavailable")


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def no_window_flags(below_normal: bool = True) -> dict[str, Any]:
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if below_normal:
        flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    return {"creationflags": flags}


def lower_priority(pid: int) -> None:
    try:
        import psutil

        p = psutil.Process(pid)
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(5)
    except Exception:  # noqa: BLE001
        pass


def runtime_manifest(runtime_dir: Path) -> dict[str, Any]:
    f = runtime_dir / "manifest.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def find_executable(runtime_dir: Path, component: str, names: list[str]) -> Path | None:
    man = runtime_manifest(runtime_dir).get(component, {})
    if man.get("exe"):
        p = runtime_dir / man["exe"]
        if p.exists():
            return p
    base = runtime_dir / component
    exe_names = [n + (".exe" if os.name == "nt" else "") for n in names]
    if base.exists():
        for name in exe_names:
            hits = sorted(base.rglob(name))
            if hits:
                return hits[-1]
    for name in names:
        found = shutil.which(name)
        if found and os.environ.get("NEXTAI_ALLOW_PATH_BINARIES") == "1":
            return Path(found)
    return None


def python_exe() -> str:
    return sys.executable
