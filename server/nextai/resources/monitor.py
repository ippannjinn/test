"""Host resource sampling: CPU, RAM, disk, NVIDIA GPU (NVML → nvidia-smi fallback) or a mock GPU."""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import psutil

log = logging.getLogger("nextai.resources")


@dataclass
class GpuInfo:
    index: int
    name: str
    vram_total_mb: int
    vram_used_mb: int
    util_percent: float = 0.0
    temp_c: float | None = None
    power_w: float | None = None
    driver: str = ""
    cuda_version: str = ""


@dataclass
class Snapshot:
    ts: float
    cpu_percent: float
    cpu_count: int
    ram_total_mb: int
    ram_available_mb: int
    disk_total_gb: float
    disk_free_gb: float
    gpus: list[GpuInfo] = field(default_factory=list)
    own_vram_mb: int = 0
    own_ram_mb: int = 0
    gpu_provider: str = "none"

    @property
    def gpu(self) -> GpuInfo | None:
        return self.gpus[0] if self.gpus else None

    @property
    def external_vram_mb(self) -> int:
        g = self.gpu
        return max(0, g.vram_used_mb - self.own_vram_mb) if g else 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["external_vram_mb"] = self.external_vram_mb
        d["ram_used_percent"] = round(100 * (1 - self.ram_available_mb / max(1, self.ram_total_mb)), 1)
        return d


class GpuProvider:
    name = "none"

    def read(self) -> list[GpuInfo]:
        return []

    def close(self) -> None:
        pass


class NvmlProvider(GpuProvider):
    name = "nvml"

    def __init__(self) -> None:
        import pynvml  # nvidia-ml-py

        self.nv = pynvml
        pynvml.nvmlInit()
        self.count = pynvml.nvmlDeviceGetCount()
        if self.count == 0:
            raise RuntimeError("no NVIDIA GPU")
        self.driver = _s(pynvml.nvmlSystemGetDriverVersion())
        try:
            v = pynvml.nvmlSystemGetCudaDriverVersion()
            self.cuda = f"{v // 1000}.{(v % 1000) // 10}"
        except pynvml.NVMLError:
            self.cuda = ""

    def read(self) -> list[GpuInfo]:
        nv, out = self.nv, []
        for i in range(self.count):
            h = nv.nvmlDeviceGetHandleByIndex(i)
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            try:
                util = float(nv.nvmlDeviceGetUtilizationRates(h).gpu)
            except nv.NVMLError:
                util = 0.0
            try:
                temp = float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU))
            except nv.NVMLError:
                temp = None
            try:
                power = nv.nvmlDeviceGetPowerUsage(h) / 1000.0
            except nv.NVMLError:
                power = None
            out.append(GpuInfo(i, _s(nv.nvmlDeviceGetName(h)), int(mem.total / 2**20), int(mem.used / 2**20),
                               util, temp, power, self.driver, self.cuda))
        return out

    def close(self) -> None:
        try:
            self.nv.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


class NvidiaSmiProvider(GpuProvider):
    name = "nvidia-smi"
    QUERY = "index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,driver_version"

    def __init__(self) -> None:
        self.exe = shutil.which("nvidia-smi")
        if not self.exe:
            raise RuntimeError("nvidia-smi not found")
        self.cuda = ""
        try:
            head = subprocess.run([self.exe], capture_output=True, text=True, timeout=10).stdout
            if "CUDA Version:" in head:
                self.cuda = head.split("CUDA Version:")[1].split()[0].strip("| ")
        except (OSError, subprocess.SubprocessError):
            pass
        if not self.read():
            raise RuntimeError("nvidia-smi returned no GPUs")

    def read(self) -> list[GpuInfo]:
        try:
            res = subprocess.run([self.exe, f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10, **_no_window())
        except (OSError, subprocess.SubprocessError):
            return []
        out = []
        for line in res.stdout.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 8:
                continue
            out.append(GpuInfo(int(p[0]), p[1], _int(p[2]), _int(p[3]), _float(p[4]) or 0.0, _float(p[5]),
                               _float(p[6]), p[7], self.cuda))
        return out


class MockGpuProvider(GpuProvider):
    """Simulated 12GB GPU used in tests and cloud CI. Mock backends register allocations here."""
    name = "mock"

    def __init__(self, total_mb: int = 12227, baseline_mb: int = 600, name: str = "Mock RTX 5070 (simulated)"):
        self.total_mb, self.baseline_mb, self.gpu_name = total_mb, baseline_mb, name
        self.allocations: dict[str, int] = {}
        self.external_mb = 0
        self.temp_c = 45.0
        self.active = 0
        self._lock = threading.Lock()

    def allocate(self, key: str, mb: int) -> None:
        with self._lock:
            self.allocations[key] = int(mb)

    def free(self, key: str) -> None:
        with self._lock:
            self.allocations.pop(key, None)

    def read(self) -> list[GpuInfo]:
        with self._lock:
            used = self.baseline_mb + self.external_mb + sum(self.allocations.values())
        util = min(100.0, self.active * 45.0)
        return [GpuInfo(0, self.gpu_name, self.total_mb, min(used, self.total_mb), util, self.temp_c, 60.0 + util,
                        "mock", "12.8")]


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _int(s: str) -> int:
    try:
        return int(float(s))
    except ValueError:
        return 0


def _float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def _no_window() -> dict:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def detect_gpu_provider(mode: str = "auto") -> GpuProvider:
    if mode == "mock":
        return MockGpuProvider()
    if mode == "none":
        return GpuProvider()
    for cls in (NvmlProvider, NvidiaSmiProvider):
        if mode not in ("auto", cls.name):
            continue
        try:
            return cls()
        except Exception as e:  # noqa: BLE001
            log.info("GPU provider %s unavailable: %s", cls.name, e)
    return GpuProvider()


class ResourceMonitor:
    def __init__(self, disk_path: Path, gpu: GpuProvider, interval: float = 2.0, history: int = 900):
        self.disk_path = Path(disk_path)
        self.gpu = gpu
        self.interval = interval
        self.history: deque[Snapshot] = deque(maxlen=history)
        self.own_vram_fn: Callable[[], int] = lambda: 0
        self.listeners: list[Callable[[Snapshot], None]] = []
        self._task: asyncio.Task | None = None
        self._latest: Snapshot | None = None
        psutil.cpu_percent(interval=None)

    def sample(self) -> Snapshot:
        vm = psutil.virtual_memory()
        try:
            du = shutil.disk_usage(self.disk_path)
            disk_total, disk_free = du.total / 2**30, du.free / 2**30
        except OSError:
            disk_total = disk_free = 0.0
        try:
            gpus = self.gpu.read()
        except Exception as e:  # noqa: BLE001
            log.warning("GPU read failed: %s", e)
            gpus = []
        snap = Snapshot(ts=time.time(), cpu_percent=psutil.cpu_percent(interval=None),
                        cpu_count=psutil.cpu_count(logical=True) or 1, ram_total_mb=int(vm.total / 2**20),
                        ram_available_mb=int(vm.available / 2**20), disk_total_gb=round(disk_total, 2),
                        disk_free_gb=round(disk_free, 2), gpus=gpus, own_vram_mb=int(self.own_vram_fn()),
                        own_ram_mb=_own_ram_mb(),
                        gpu_provider=self.gpu.name)
        self._latest = snap
        return snap

    @property
    def latest(self) -> Snapshot:
        return self._latest or self.sample()

    def recent(self, n: int = 300) -> list[Snapshot]:
        return list(self.history)[-n:]

    async def _run(self) -> None:
        while True:
            try:
                snap = await asyncio.to_thread(self.sample)
                self.history.append(snap)
                for cb in list(self.listeners):
                    try:
                        cb(snap)
                    except Exception:  # noqa: BLE001
                        log.exception("resource listener failed")
            except Exception:  # noqa: BLE001
                log.exception("resource sampling failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="resource-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.gpu.close()


def _own_ram_mb() -> int:
    """RAM held by the server and its workers (llama-server, sd, ...): resident set of the process tree."""
    try:
        me = psutil.Process()
        total = me.memory_info().rss
        for c in me.children(recursive=True):
            try:
                total += c.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return int(total / 2**20)
    except Exception:  # noqa: BLE001
        return 0
