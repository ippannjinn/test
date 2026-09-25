"""Inference runtime installation on the target PC (llama.cpp, stable-diffusion.cpp, python.wasm, PyTorch env).

Binaries are resolved from the projects' GitHub releases at install time and the best build for the
detected driver is chosen (CUDA ≤ driver version → Vulkan → CPU). After install, a self-test verifies that
llama.cpp actually sees the GPU; if the CUDA build cannot, the Vulkan build is installed instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from ..backends.base import no_window_flags
from ..config import Settings
from .downloader import Downloader, DownloadError, RemoteFile, ensure_space, make_client

Event = Callable[[dict], None]
GITHUB = "https://api.github.com"
IS_WIN = os.name == "nt"


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:2]) or (0,)


def driver_cuda_version() -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=15, **no_window_flags(False)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"CUDA Version:\s*([\d.]+)", out)
    return m.group(1) if m else None


def pick_llama_assets(assets: list[dict], cuda: str | None, prefer: str = "auto") -> tuple[str, dict, dict | None]:
    """Returns (accel, main_asset, cudart_asset_or_None)."""
    names = {a["name"]: a for a in assets}
    if IS_WIN:
        cuda_rx = re.compile(r"^llama-.*-bin-win-cuda-?([\d.]+)-x64\.zip$")
        cudart_rx = re.compile(r"^cudart-llama-bin-win-cuda-?([\d.]+)-x64\.zip$")
        vulkan_rx = re.compile(r"^llama-.*-bin-win-vulkan-x64\.zip$")
        cpu_rx = re.compile(r"^llama-.*-bin-win-(cpu|avx2)-x64\.zip$")
    else:
        cuda_rx = re.compile(r"^llama-.*-bin-ubuntu-cuda-?([\d.]+)-x64\.zip$")
        cudart_rx = re.compile(r"^$")
        vulkan_rx = re.compile(r"^llama-.*-bin-ubuntu-vulkan-x64\.zip$")
        cpu_rx = re.compile(r"^llama-.*-bin-ubuntu-x64\.zip$")
    if cuda and prefer in ("auto", "cuda"):
        builds = sorted(((_ver(m.group(1)), n) for n in names if (m := cuda_rx.match(n))), reverse=True)
        usable = [(v, n) for v, n in builds if v <= _ver(cuda)]
        if usable:
            v, n = usable[0]
            cudart = next((names[c] for c in names if (m := cudart_rx.match(c)) and _ver(m.group(1)) == v), None)
            return f"cuda-{'.'.join(map(str, v))}", names[n], cudart
    if prefer in ("auto", "cuda", "vulkan"):
        vk = next((n for n in names if vulkan_rx.match(n)), None)
        if vk:
            return "vulkan", names[vk], None
    cpu = next((n for n in names if cpu_rx.match(n)), None)
    if cpu:
        return "cpu", names[cpu], None
    raise DownloadError("llama.cpp の適切なビルドが見つかりません")


def pick_sd_assets(assets: list[dict], cuda: str | None, prefer: str = "auto") -> tuple[str, dict, dict | None]:
    names = {a["name"]: a for a in assets}
    plat = "win" if IS_WIN else "(linux|ubuntu)"
    cuda_rx = re.compile(rf"^sd-.*-bin-{plat}-cuda-?(\d+[\d.]*)-x64\.zip$")
    cudart_rx = re.compile(r"^cudart-sd-bin-win-cu-?(\d+)[\d.]*-x64\.zip$")
    if cuda and prefer in ("auto", "cuda"):
        builds = sorted(((_ver(m.group(m.lastindex)), n) for n in names if (m := cuda_rx.match(n))), reverse=True)
        usable = [(v, n) for v, n in builds if v[:1] <= _ver(cuda)[:1]]
        if usable:
            v, n = usable[0]
            cudart = next((names[c] for c in names if (m := cudart_rx.match(c)) and _ver(m.group(1))[:1] == v[:1]), None)
            return f"cuda{v[0]}", names[n], cudart
    for accel, rx in (("vulkan", rf"^sd-.*-bin-{plat}-vulkan-x64\.zip$"), ("cpu", rf"^sd-.*-bin-{plat}-avx2-x64\.zip$"),
                      ("cpu", rf"^sd-.*-bin-{plat}-(avx|noavx)?-?x64\.zip$")):
        n = next((x for x in names if re.match(rx, x)), None)
        if n:
            return accel, names[n], None
    raise DownloadError("stable-diffusion.cpp の適切なビルドが見つかりません")


class RuntimeInstaller:
    def __init__(self, settings: Settings, emit: Event, client: httpx.Client | None = None):
        self.settings, self.emit = settings, emit
        self.client = client or make_client()
        self.rt = settings.paths.runtime
        self.rt.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.rt / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text("utf-8")) if self.manifest_path.exists() else {}

    def _save(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False), "utf-8")

    def _releases(self, repo: str) -> list[dict]:
        r = self.client.get(f"{GITHUB}/repos/{repo}/releases?per_page=30", headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return [x for x in r.json() if not x.get("draft")]

    def _download_asset(self, asset: dict, dest_dir: Path) -> Path:
        dest = self.rt / "downloads" / asset["name"]
        digest = (asset.get("digest") or "").removeprefix("sha256:") or None
        rf = RemoteFile(url=asset["browser_download_url"], path=asset["name"], size=asset.get("size"), sha256=digest)

        def prog(name: str, done: int, size: int) -> None:
            self.emit({"event": "progress", "component": dest_dir.name, "file": name, "done": done, "size": size})

        Downloader(self.client, prog, segments=4).download(rf, dest)
        return dest

    @staticmethod
    def _extract(zpath: Path, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():
                if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                    raise DownloadError(f"不正なアーカイブ: {n}")
            z.extractall(tmp)
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)

    def _find(self, base: Path, names: list[str]) -> Path | None:
        for n in names:
            hits = sorted(base.rglob(n + (".exe" if IS_WIN else "")))
            if hits:
                return hits[0]
        return None

    def install_llama(self, prefer: str = "auto") -> dict:
        cuda = driver_cuda_version()
        rel = self._releases("ggml-org/llama.cpp")[0]
        accel, main, cudart = pick_llama_assets(rel["assets"], cuda, prefer)
        self.emit({"event": "component", "component": "llama.cpp", "version": rel["tag_name"], "accel": accel})
        dest = self.rt / "llama.cpp" / f"{rel['tag_name']}-{accel}"
        self._extract(self._download_asset(main, dest), dest)
        if cudart:
            with zipfile.ZipFile(self._download_asset(cudart, dest)) as z:
                exe_dir = (self._find(dest, ["llama-server"]) or dest).parent
                for n in z.namelist():
                    if n.lower().endswith(".dll") and ".." not in n:
                        (exe_dir / Path(n).name).write_bytes(z.read(n))
        exe = self._find(dest, ["llama-server"])
        if not exe:
            raise DownloadError("llama-server が見つかりません")
        if not IS_WIN:
            exe.chmod(0o755)
        entry = {"version": rel["tag_name"], "accel": accel, "dir": dest.relative_to(self.rt).as_posix(),
                 "exe": exe.relative_to(self.rt).as_posix()}
        ok, devices = self.self_test_llama(exe)
        entry["devices"] = devices
        if accel.startswith("cuda") and not ok and prefer == "auto":
            self.emit({"event": "notice", "message": "CUDAビルドでGPUを検出できないため Vulkan ビルドに切り替えます"})
            return self.install_llama("vulkan")
        self.manifest["llama.cpp"] = entry
        self._save()
        return entry

    @staticmethod
    def self_test_llama(exe: Path) -> tuple[bool, str]:
        try:
            r = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=60,
                               cwd=str(exe.parent), **no_window_flags(False))
            text = (r.stdout + r.stderr)[-2000:]
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        gpu = bool(re.search(r"(CUDA\d|Vulkan\d|NVIDIA|GeForce|RTX)", text))
        return gpu, text.strip()[-500:]

    def install_sd(self, prefer: str = "auto") -> dict:
        cuda = driver_cuda_version()
        rel = self._releases("leejet/stable-diffusion.cpp")[0]
        accel, main, cudart = pick_sd_assets(rel["assets"], cuda, prefer)
        self.emit({"event": "component", "component": "sd.cpp", "version": rel["tag_name"], "accel": accel})
        dest = self.rt / "sd.cpp" / f"{rel['tag_name']}-{accel}"
        self._extract(self._download_asset(main, dest), dest)
        exe = self._find(dest, ["sd", "sd-cli"])
        if not exe:
            raise DownloadError("sd 実行ファイルが見つかりません")
        if cudart:
            with zipfile.ZipFile(self._download_asset(cudart, dest)) as z:
                for n in z.namelist():
                    if n.lower().endswith(".dll") and ".." not in n:
                        (exe.parent / Path(n).name).write_bytes(z.read(n))
        if not IS_WIN:
            exe.chmod(0o755)
        entry = {"version": rel["tag_name"], "accel": accel, "dir": dest.relative_to(self.rt).as_posix(),
                 "exe": exe.relative_to(self.rt).as_posix()}
        self.manifest["sd.cpp"] = entry
        self._save()
        return entry

    def install_python_wasm(self) -> dict:
        rels = self._releases("vmware-labs/webassembly-language-runtimes")
        for rel in rels:
            if not rel["tag_name"].startswith("python/3."):
                continue
            asset = next((a for a in rel["assets"] if re.match(r"^python-3\.\d+\.\d+\.wasm$", a["name"])), None)
            if asset:
                dest = self.rt / "python-wasm"
                dest.mkdir(parents=True, exist_ok=True)
                src = self._download_asset(asset, dest)
                target = dest / asset["name"]
                shutil.copyfile(src, target)
                entry = {"version": rel["tag_name"], "path": target.relative_to(self.rt).as_posix()}
                self.manifest["python-wasm"] = entry
                self._save()
                self.emit({"event": "component", "component": "python-wasm", "version": rel["tag_name"]})
                return entry
        raise DownloadError("python.wasm のリリースが見つかりません")

    def install_musicgen(self, uv: str | None = None) -> dict:
        uv = uv or os.environ.get("NEXTAI_UV") or shutil.which("uv")
        if not uv:
            raise DownloadError("uv が見つかりません")
        env_dir = self.rt / "genai"
        env = dict(os.environ)
        env.setdefault("UV_PYTHON_INSTALL_DIR", str(self.rt / "python"))
        env.setdefault("UV_CACHE_DIR", str(self.rt / "uv-cache"))
        self.emit({"event": "component", "component": "musicgen", "version": "torch-cu128"})
        steps = [[uv, "venv", "--python", "3.12", "--python-preference", "only-managed", str(env_dir)],
                 [uv, "pip", "install", "--python", str(env_dir), "--index-url", "https://download.pytorch.org/whl/cu128",
                  "torch==2.7.1"],
                 [uv, "pip", "install", "--python", str(env_dir), "transformers>=4.46,<5", "scipy", "numpy",
                  "sentencepiece", "safetensors"]]
        for cmd in steps:
            self.emit({"event": "step", "message": " ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd[:4]))})
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, **no_window_flags(False))
            if r.returncode != 0:
                raise DownloadError(f"PyTorch環境の構築に失敗しました: {r.stderr[-400:]}")
        py = env_dir / ("Scripts/python.exe" if IS_WIN else "bin/python")
        entry = {"python": py.relative_to(self.rt).as_posix(), "version": "torch-2.7.1-cu128"}
        self.manifest["musicgen"] = entry
        self._save()
        return entry

    def install(self, components: list[str]) -> dict:
        ensure_space(self.rt, 2 * 2**30 + (5 * 2**30 if "musicgen" in components else 0),
                     self.settings.resources.disk_margin_gb)
        results = {}
        for c in components:
            fn = {"llama.cpp": self.install_llama, "sd.cpp": self.install_sd, "python-wasm": self.install_python_wasm,
                  "musicgen": self.install_musicgen}.get(c)
            if fn is None:
                results[c] = "unknown component"
                continue
            try:
                results[c] = fn()
                self.emit({"event": "component_done", "component": c})
            except (DownloadError, httpx.HTTPError, OSError, subprocess.SubprocessError) as e:
                results[c] = f"error: {e}"
                self.emit({"event": "component_error", "component": c, "error": str(e)})
        return results


def python_exe() -> str:
    return sys.executable
