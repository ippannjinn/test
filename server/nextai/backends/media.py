"""stable-diffusion.cpp (image/video) and MusicGen (music) subprocess backends."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import Settings
from ..models.catalog import ModelSpec
from ..models.planner import LaunchPlan
from .base import BackendError, MediaBackend, ProgressFn, find_executable, lower_priority, no_window_flags, runtime_manifest

log = logging.getLogger("nextai.media")
_STEP_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _run_process(args: list[str], cwd: Path, log_path: Path, timeout: float, cancel: threading.Event,
                 on_line) -> int:
    with open(log_path, "ab") as logf:
        logf.write(f"\n==== {time.ctime()} {' '.join(args[:3])} ...\n".encode())
        proc = subprocess.Popen(args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **no_window_flags())
        lower_priority(proc.pid)
        deadline = time.time() + timeout

        def pump():
            assert proc.stdout is not None
            buf = b""
            while True:
                chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    break
                logf.write(chunk)
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    if p.strip():
                        on_line(p.decode("utf-8", "replace"))

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        while proc.poll() is None:
            if cancel.is_set() or time.time() > deadline:
                proc.kill()
                proc.wait(10)
                t.join(2)
                raise BackendError("キャンセルされました" if cancel.is_set() else "生成がタイムアウトしました")
            time.sleep(0.2)
        t.join(5)
        return proc.returncode


def frames_to_webp(frames: list[Image.Image], out: Path, fps: int) -> Path:
    if not frames:
        raise BackendError("動画フレームが生成されませんでした")
    frames = [f.convert("RGB") for f in frames]
    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:], duration=int(1000 / max(1, fps)),
                   loop=0, quality=80)
    return out


def extract_mjpeg_frames(avi: Path) -> list[Image.Image]:
    """Minimal MJPEG-in-AVI reader: JPEG frames are stored verbatim between SOI/EOI markers."""
    import io

    data = avi.read_bytes()
    frames, pos = [], 0
    while True:
        start = data.find(b"\xff\xd8\xff", pos)
        if start < 0:
            break
        end = data.find(b"\xff\xd9", start + 3)
        if end < 0:
            break
        try:
            frames.append(Image.open(io.BytesIO(data[start:end + 2])).copy())
        except OSError:
            pass
        pos = end + 2
    return frames


class SdCppBackend(MediaBackend):
    name = "sdcpp"
    kinds = ("image", "video")

    def __init__(self, settings: Settings, runtime_dir: Path, log_dir: Path):
        self.settings = settings
        self.log_dir = log_dir
        self.exe = find_executable(runtime_dir, "sd.cpp", ["sd", "sd-cli"])
        self.available = self.exe is not None
        self._help: str | None = None

    def help_text(self) -> str:
        if self._help is None:
            try:
                r = subprocess.run([str(self.exe), "--help"], capture_output=True, text=True, timeout=30,
                                   errors="replace", **no_window_flags(False))
                self._help = (r.stdout or "") + (r.stderr or "")
            except (OSError, subprocess.SubprocessError):
                self._help = ""
        return self._help

    async def generate(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan | None,
                       params: dict[str, Any], out_dir: Path, progress: ProgressFn, cancel: threading.Event) -> list[Path]:
        if not self.available:
            raise BackendError("stable-diffusion.cpp ランタイムがインストールされていません")
        out_dir.mkdir(parents=True, exist_ok=True)
        is_video = spec.kind == "video"
        output = out_dir / ("out.avi" if is_video else "out.png")
        mapping = {k: str(v) for k, v in paths.items()}
        mapping.update({k: str(v) for k, v in params.items()})
        mapping["output"] = str(output)
        mapping.setdefault("negative", "")
        args = [str(self.exe)] + [a.format_map(mapping) for a in spec.args]
        help_text = self.help_text()
        args += [a for a in spec.optional_args if a in help_text]
        if plan and plan.offload and "--offload-to-cpu" in help_text:
            args.append("--offload-to-cpu")
        total_steps = int(params.get("steps", 20))

        def on_line(line: str) -> None:
            m = _STEP_RE.search(line)
            if m and ("it/s" in line or "s/it" in line or "|" in line):
                done, total = int(m.group(1)), int(m.group(2))
                if 0 < total <= 10000:
                    progress(min(0.98, done / total), f"step {done}/{total}")

        timeout = self.settings.generation.video_timeout_seconds if is_video else 900
        progress(0.01, "モデル読み込み中")
        rc = await asyncio.to_thread(_run_process, args, out_dir, self.log_dir / "sdcpp.log", timeout, cancel, on_line)
        if rc != 0:
            raise BackendError(f"stable-diffusion.cpp がエラー終了しました (exit {rc})")
        if not is_video:
            pngs = sorted(out_dir.glob("out*.png"))
            if not pngs:
                raise BackendError("画像が出力されませんでした")
            return pngs
        fps = int(params.get("fps", spec.defaults.get("fps", 16)))
        for ext in ("webm", "mp4"):
            vids = sorted(out_dir.glob(f"out*.{ext}"))
            if vids:
                return vids
        frames: list[Image.Image] = []
        if output.exists():
            frames = extract_mjpeg_frames(output)
        if not frames:
            frames = [Image.open(p) for p in sorted(out_dir.glob("out*.png"))]
        webp = frames_to_webp(frames, out_dir / "video.webp", fps)
        progress(1.0, f"{len(frames)} frames (total_steps={total_steps})")
        return [webp]


class MusicGenBackend(MediaBackend):
    name = "musicgen"
    kinds = ("music",)

    def __init__(self, settings: Settings, runtime_dir: Path, log_dir: Path):
        self.settings = settings
        self.log_dir = log_dir
        man = runtime_manifest(runtime_dir).get("musicgen", {})
        py = runtime_dir / man["python"] if man.get("python") else None
        self.python = py if py and py.exists() else None
        self.available = self.python is not None
        self.worker = Path(__file__).with_name("musicgen_worker.py")

    async def generate(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan | None,
                       params: dict[str, Any], out_dir: Path, progress: ProgressFn, cancel: threading.Event) -> list[Path]:
        if not self.available:
            raise BackendError("音楽生成ランタイム(PyTorch)がインストールされていません")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "music.wav"
        model_dir = paths["snapshot"] if paths["snapshot"].is_dir() else paths["snapshot"].parent
        args = [str(self.python), str(self.worker), "--model-dir", str(model_dir), "--prompt", str(params["prompt"]),
                "--seconds", str(params["seconds"]), "--seed", str(params.get("seed", 0)), "--out", str(out)]
        if plan is None or not plan.gpu:
            args.append("--cpu")

        def on_line(line: str) -> None:
            try:
                obj = json.loads(line)
            except ValueError:
                return
            if "progress" in obj:
                progress(float(obj["progress"]), obj.get("message", ""))

        rc = await asyncio.to_thread(_run_process, args, out_dir, self.log_dir / "musicgen.log", 900, cancel, on_line)
        if rc != 0 or not out.exists():
            raise BackendError(f"音楽生成に失敗しました (exit {rc})")
        return [out]


def env_for_worker() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return env
