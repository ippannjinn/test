"""Isolated code execution.

Backends:
  wasm    — CPython compiled to WASI, run by wasmtime. No network, no processes, filesystem limited to
            one pre-opened per-session directory, memory capped by the store, time capped by epochs.
            Works natively on Windows; this is the default on the target PC.
  process — Linux-only fallback (dev/CI/WSL): rlimits (CPU, address space, file size, processes),
            empty environment, private temp dir and `unshare --net` when available.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..backends.base import runtime_manifest
from ..config import Settings

log = logging.getLogger("nextai.sandbox")

RUNNER = """import os, sys, runpy
try:
    os.chdir({cwd!r})
except Exception:
    pass
sys.argv = ['main.py']
runpy.run_path('main.py', run_name='__main__')
"""


@dataclass
class SandboxResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration: float = 0.0
    backend: str = ""
    files: list[str] = field(default_factory=list)
    error: str = ""
    artifacts: dict[str, bytes] = field(default_factory=dict)

    def to_text(self, limit: int = 8000) -> str:
        parts = [f"exit_code={self.exit_code}{' (timeout)' if self.timed_out else ''} backend={self.backend}"]
        if self.stdout:
            parts.append("stdout:\n" + self.stdout[:limit])
        if self.stderr:
            parts.append("stderr:\n" + self.stderr[:limit // 2])
        if self.error:
            parts.append("error: " + self.error)
        if self.files:
            parts.append("files: " + ", ".join(self.files[:50]))
        return "\n".join(parts)


def _read_limited(p: Path, limit: int) -> str:
    if not p.exists():
        return ""
    with open(p, "rb") as f:
        data = f.read(limit + 1)
    text = data[:limit].decode("utf-8", "replace")
    return text + ("\n…(出力が長すぎるため省略)" if len(data) > limit else "")


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


class WasmPythonBackend:
    name = "wasm"
    TICK = 0.05

    def __init__(self, wasm_path: Path, cache_dir: Path):
        self.wasm_path = wasm_path
        self.cache_dir = cache_dir
        self.available = wasm_path.exists()
        self._engine = None
        self._module = None
        self._lock = threading.Lock()
        self._ticker: threading.Thread | None = None

    def _ensure(self):
        with self._lock:
            if self._module is not None:
                return
            import wasmtime

            cfg = wasmtime.Config()
            cfg.epoch_interruption = True
            self._engine = wasmtime.Engine(cfg)
            st = self.wasm_path.stat()
            key = hashlib.sha256(f"{self.wasm_path}:{st.st_size}:{st.st_mtime}".encode()).hexdigest()[:16]
            cached = self.cache_dir / f"python-{key}.cwasm"
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            module = None
            if cached.exists():
                try:
                    module = wasmtime.Module.deserialize_file(self._engine, str(cached))
                except Exception:  # noqa: BLE001
                    module = None
            if module is None:
                module = wasmtime.Module.from_file(self._engine, str(self.wasm_path))
                try:
                    cached.write_bytes(module.serialize())
                except OSError:
                    pass
            self._module = module
            self._ticker = threading.Thread(target=self._tick, daemon=True, name="wasm-epoch")
            self._ticker.start()

    def _tick(self) -> None:
        while True:
            time.sleep(self.TICK)
            self._engine.increment_epoch()

    def run_sync(self, workdir: Path, timeout: float, memory_mb: int, max_output: int) -> SandboxResult:
        import wasmtime

        self._ensure()
        (workdir / "__nextai_run__.py").write_text(RUNNER.format(cwd="/workspace"), encoding="utf-8")
        out_f = workdir.parent / f".{workdir.name}.stdout"
        err_f = workdir.parent / f".{workdir.name}.stderr"
        store = wasmtime.Store(self._engine)
        store.set_limits(memory_size=memory_mb * 2**20, instances=10, tables=10, memories=2)
        store.set_epoch_deadline(max(1, int(timeout / self.TICK)))
        wasi = wasmtime.WasiConfig()
        wasi.argv = ["python", "-B", "/workspace/__nextai_run__.py"]
        wasi.env = [("PYTHONDONTWRITEBYTECODE", "1"), ("HOME", "/workspace"), ("PYTHONIOENCODING", "utf-8")]
        wasi.preopen_dir(str(workdir), "/workspace")
        wasi.stdout_file = str(out_f)
        wasi.stderr_file = str(err_f)
        store.set_wasi(wasi)
        linker = wasmtime.Linker(self._engine)
        linker.define_wasi()
        t0 = time.time()
        code, timed_out, error = 0, False, ""
        try:
            inst = linker.instantiate(store, self._module)
            inst.exports(store)["_start"](store)
        except wasmtime.ExitTrap as e:
            code = e.code
        except wasmtime.Trap as e:
            msg = str(e)
            if "interrupt" in msg.lower() or "epoch" in msg.lower():
                timed_out, code, error = True, 124, "時間制限を超えました"
            else:
                code, error = 134, msg.splitlines()[0][:300]
        except wasmtime.WasmtimeError as e:
            code, error = 134, str(e).splitlines()[0][:300]
        dur = time.time() - t0
        (workdir / "__nextai_run__.py").unlink(missing_ok=True)
        res = SandboxResult(code == 0 and not timed_out, code, _read_limited(out_f, max_output),
                            _read_limited(err_f, max_output // 2), timed_out, dur, self.name, error=error)
        out_f.unlink(missing_ok=True)
        err_f.unlink(missing_ok=True)
        return res


class ProcessBackend:
    name = "process"

    def __init__(self):
        self.available = os.name != "nt"
        self._unshare = self._probe_unshare() if self.available else False

    @staticmethod
    def _probe_unshare() -> bool:
        exe = shutil.which("unshare")
        if not exe:
            return False
        try:
            return subprocess.run([exe, "--net", "--", "true"], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def run_sync(self, workdir: Path, timeout: float, memory_mb: int, max_output: int, disk_mb: int = 100) -> SandboxResult:
        import resource

        def limits():
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (int(timeout) + 1, int(timeout) + 2))
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 2**20, memory_mb * 2**20))
            resource.setrlimit(resource.RLIMIT_FSIZE, (disk_mb * 2**20, disk_mb * 2**20))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            if os.geteuid() != 0:
                resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))

        (workdir / "__nextai_run__.py").write_text(RUNNER.format(cwd=str(workdir)), encoding="utf-8")
        cmd = [sys.executable, "-I", "-B", str(workdir / "__nextai_run__.py")]
        if self._unshare:
            cmd = [shutil.which("unshare"), "--net", "--"] + cmd
        env = {"PATH": "/usr/bin:/bin", "HOME": str(workdir), "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}
        t0 = time.time()
        timed_out = False
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(cmd, cwd=str(workdir), stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                                    preexec_fn=limits)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    proc.kill()
                proc.wait(5)
            out.seek(0)
            err.seek(0)
            so = out.read(max_output + 1)
            se = err.read(max_output // 2 + 1)
        (workdir / "__nextai_run__.py").unlink(missing_ok=True)
        code = 124 if timed_out else proc.returncode
        return SandboxResult(code == 0, code, so[:max_output].decode("utf-8", "replace"),
                             se[:max_output // 2].decode("utf-8", "replace"), timed_out, time.time() - t0,
                             self.name + ("+netns" if self._unshare else ""),
                             error="時間制限を超えました" if timed_out else "")


class SandboxManager:
    def __init__(self, settings: Settings, governor=None):
        self.settings = settings
        self.governor = governor
        rt = settings.paths.runtime
        man = runtime_manifest(rt).get("python-wasm", {})
        wasm = rt / man["path"] if man.get("path") else rt / "python-wasm" / "python.wasm"
        mode = settings.sandbox.backend
        self.backend = None
        if mode in ("auto", "wasm"):
            wb = WasmPythonBackend(wasm, rt / "cache")
            if wb.available:
                self.backend = wb
        if self.backend is None and mode in ("auto", "process") and os.name != "nt":
            self.backend = ProcessBackend()
        self._sem = asyncio.Semaphore(max(1, settings.sandbox.max_concurrent))

    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def name(self) -> str:
        return self.backend.name if self.backend else "disabled"

    async def run(self, user_id: str, code: str, *, workspace: Path | None = None,
                  files: dict[str, bytes] | None = None) -> SandboxResult:
        if not self.backend:
            return SandboxResult(False, -1, "", "", backend="disabled",
                                 error="コード実行環境がインストールされていません (python.wasm)")
        if self.governor is not None and self.governor.state.sandbox_factor <= 0:
            return SandboxResult(False, -1, "", "", backend=self.name, error="高負荷のためコード実行を一時停止しています")
        s = self.settings.sandbox
        base = self.settings.paths.users / user_id / "sandbox"
        base.mkdir(parents=True, exist_ok=True)
        ephemeral = workspace is None
        workdir = Path(tempfile.mkdtemp(prefix="run-", dir=base)) if ephemeral else workspace
        try:
            before = {p.relative_to(workdir).as_posix(): p.stat().st_mtime for p in workdir.rglob("*") if p.is_file()}
            (workdir / "main.py").write_text(code, encoding="utf-8")
            for name, data in (files or {}).items():
                safe = Path(name).name
                if safe and safe not in ("main.py", "__nextai_run__.py"):
                    (workdir / safe).write_bytes(data)
            async with self._sem:
                if isinstance(self.backend, ProcessBackend):
                    res = await asyncio.to_thread(self.backend.run_sync, workdir, s.timeout_seconds, s.memory_mb,
                                                  s.max_output_kb * 1024, s.disk_mb)
                else:
                    res = await asyncio.to_thread(self.backend.run_sync, workdir, s.timeout_seconds, s.memory_mb,
                                                  s.max_output_kb * 1024)
            if _dir_size(workdir) > s.disk_mb * 2**20:
                res.ok, res.error = False, f"ディスク上限 ({s.disk_mb}MB) を超えたため出力を破棄しました"
                if ephemeral:
                    shutil.rmtree(workdir, ignore_errors=True)
                    ephemeral = False
                return res
            res.files = sorted(p.relative_to(workdir).as_posix() for p in workdir.rglob("*")
                               if p.is_file() and p.name != "main.py"
                               and before.get(p.relative_to(workdir).as_posix()) != p.stat().st_mtime)
            if ephemeral:
                for rel in res.files[:10]:
                    fp = workdir / rel
                    if fp.stat().st_size <= 5 * 2**20:
                        res.artifacts[rel] = fp.read_bytes()
            return res
        finally:
            if ephemeral:
                shutil.rmtree(workdir, ignore_errors=True)
