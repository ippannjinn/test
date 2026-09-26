"""On-device diagnostics and benchmarks (PASS / WARN / FAIL).

Everything hardware-specific (VRAM use, tokens/s, load and swap times, temperatures) is measured on
the real PC by this module — nothing is assumed from the cloud build environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform as pyplatform
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

import psutil

from .backends.base import find_executable, no_window_flags, runtime_manifest
from .config import Settings
from .resources.monitor import detect_gpu_provider
from .security.ssrf import SSRFError, validate_url
from .util import dumps, now

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    id: str
    name: str
    status: str
    value: str = ""
    detail: str = ""
    advice: str = ""


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **no_window_flags(False))
        out = r.stdout + r.stderr
        text = out.decode("utf-16-le", "replace") if out[:2] == b"\xff\xfe" or b"\x00" in out[:40] else out.decode("utf-8", "replace")
        return r.returncode, text.replace("\x00", "")
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3]) or (0,)


def system_checks(settings: Settings) -> list[Check]:
    r = settings.resources
    out: list[Check] = []
    if os.name == "nt":
        wv = sys_win_version()
        build = wv.get("build", 0)
        out.append(Check("windows", "Windows", PASS if build >= 22000 else WARN, wv.get("text", ""),
                         advice="" if build >= 22000 else "Windows 11 を推奨します"))
    else:
        out.append(Check("os", "OS", PASS, f"{pyplatform.system()} {pyplatform.release()}"))
    cpu_name = pyplatform.processor() or pyplatform.machine()
    cores, threads = psutil.cpu_count(logical=False) or 0, psutil.cpu_count() or 0
    out.append(Check("cpu", "CPU", PASS if (threads or 0) >= 8 else WARN, f"{cpu_name} ({cores}C/{threads}T)",
                     advice="" if threads >= 8 else "CPU処理 (MoEのRAMオフロード等) が遅くなる可能性があります"))
    vm = psutil.virtual_memory()
    ram_gb = vm.total / 2**30
    out.append(Check("ram", "RAM", PASS if ram_gb >= 28 else WARN if ram_gb >= 15 else FAIL,
                     f"{ram_gb:.1f}GB (空き {vm.available / 2**30:.1f}GB)",
                     advice="" if ram_gb >= 28 else "RAMが少ないため大型MoEモデルのオフロードが制限されます"))
    du = shutil.disk_usage(settings.paths.data_dir)
    free = du.free / 2**30
    margin = r.disk_margin_gb
    out.append(Check("storage", "ストレージ", PASS if free >= margin + 20 else WARN if free >= margin else FAIL,
                     f"空き {free:.1f}GB / 全体 {du.total / 2**30:.0f}GB (安全マージン {margin:.0f}GB)",
                     advice="" if free >= margin + 20 else "モデル追加や生成物の保存容量が不足しています"))
    gpu = detect_gpu_provider("auto")
    gpus = gpu.read() if gpu.name != "none" else []
    if not gpus:
        out.append(Check("gpu", "GPU", FAIL, "NVIDIA GPU を検出できません",
                         advice="NVIDIA ドライバをインストールしてください (GPUなしではCPU推論のみ)"))
    else:
        g = gpus[0]
        vram_gb = g.vram_total_mb / 1024
        out.append(Check("gpu", "GPU", PASS, g.name, f"provider={gpu.name}"))
        out.append(Check("vram", "VRAM", PASS if vram_gb >= 10.5 else WARN if vram_gb >= 6 else FAIL,
                         f"{vram_gb:.1f}GB (使用中 {g.vram_used_mb}MB)"))
        drv = _ver(g.driver)
        is_blackwell = bool(re.search(r"RTX\s*50\d\d", g.name))
        need = (570,) if is_blackwell else (535,)
        out.append(Check("driver", "NVIDIA Driver", PASS if drv >= need else WARN, g.driver,
                         advice="" if drv >= need else f"ドライバ {need[0]} 以降へ更新してください"))
        cuda = _ver(g.cuda_version) if g.cuda_version else (0,)
        need_cuda = (12, 8) if is_blackwell else (12, 0)
        out.append(Check("cuda", "CUDA (ドライバ対応版)", PASS if cuda >= need_cuda else WARN, g.cuda_version or "不明",
                         advice="" if cuda >= need_cuda else "新しいドライバでCUDA対応版が上がります"))
        if g.temp_c is not None:
            out.append(Check("gpu_temp", "GPU温度", PASS if g.temp_c < r.gpu_temp_warn_c else WARN if g.temp_c < r.gpu_temp_pause_c else FAIL,
                             f"{g.temp_c:.0f}°C", advice="" if g.temp_c < r.gpu_temp_warn_c else "冷却・エアフローを確認してください"))
        gpu.close()
    if os.name == "nt":
        code, text = _run(["wsl.exe", "--status"], 20)
        if code == 0:
            out.append(Check("wsl2", "WSL2", PASS, "利用可能", "既定構成ではWSL2は不要 (ネイティブ実行)"))
        else:
            out.append(Check("wsl2", "WSL2", SKIP, "未構成", "既定構成ではWSL2は不要です (ネイティブ実行 + WASMサンドボックス)"))
    code, text = _run(["docker", "--version"], 10) if shutil.which("docker") else (-1, "")
    out.append(Check("docker", "Docker", PASS if code == 0 else SKIP, text.strip()[:80] if code == 0 else "未インストール",
                     "既定構成ではDockerは不要です"))
    rt = settings.paths.runtime
    man = runtime_manifest(rt)
    llama = find_executable(rt, "llama.cpp", ["llama-server"])
    lentry = man.get("llama.cpp", {})
    status, advice = (PASS, "") if llama else (FAIL, "インストーラーを再実行してください")
    if llama and not str(lentry.get("accel", "")).startswith("cuda"):
        from .install.runtime import driver_cuda_version

        if driver_cuda_version():
            status = WARN
            advice = ("NVIDIA GPU なのに CUDA 版ではありません (遅くなります)。"
                      + ("CUDA 版は起動テストに失敗しました: " + str(lentry["cuda_failed"].get("output", ""))[-120:]
                         if lentry.get("cuda_failed") else "アップデート (またはインストーラーの再実行) で CUDA 版に切り替わります"))
    out.append(Check("rt_llama", "推論ランタイム (llama.cpp)", status,
                     f"{lentry.get('version', '')} {lentry.get('accel', '')}".strip() if llama else "未インストール",
                     advice=advice))
    sd = find_executable(rt, "sd.cpp", ["sd", "sd-cli"])
    out.append(Check("rt_sd", "画像/動画ランタイム (sd.cpp)", PASS if sd else WARN,
                     man.get("sd.cpp", {}).get("version", "") if sd else "未インストール"))
    wasm = man.get("python-wasm", {}).get("path")
    out.append(Check("rt_wasm", "サンドボックス (python.wasm)", PASS if wasm and (rt / wasm).exists() else WARN,
                     wasm or "未インストール", advice="" if wasm else "コード実行機能が無効になります"))
    return out


def sys_win_version() -> dict[str, Any]:
    try:
        import winreg  # type: ignore

        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        get = lambda n: winreg.QueryValueEx(k, n)[0]  # noqa: E731
        build = int(get("CurrentBuild"))
        name = "Windows 11" if build >= 22000 else "Windows 10"
        edition = get("EditionID")
        disp = get("DisplayVersion") if build >= 19041 else ""
        return {"build": build, "text": f"{name} {edition} {disp} (Build {build})"}
    except Exception:  # noqa: BLE001
        return {"build": 0, "text": pyplatform.platform()}


async def service_checks(p: Any) -> list[Check]:
    out: list[Check] = []
    st = p.scheduler.stats()
    alive = p.scheduler._task is not None and not p.scheduler._task.done()
    out.append(Check("queue", "キュー / スケジューラ", PASS if alive else FAIL,
                     f"待機 {st['pending']} / 実行 {st['running']} / 完了 {st['completed']}"))
    try:
        validate_url("http://127.0.0.1:8443/")
        out.append(Check("ssrf", "SSRF防御", FAIL, "内部アドレスがブロックされません"))
    except SSRFError:
        out.append(Check("ssrf", "SSRF防御", PASS, "localhost/LANへのアクセスを遮断"))
    if p.sandbox.available:
        t0 = time.time()
        res = await p.sandbox.run("0" * 32, "print(6*7)")
        ok = res.ok and "42" in res.stdout
        out.append(Check("sandbox", "コード実行サンドボックス", PASS if ok else FAIL,
                         f"{p.sandbox.name} {time.time() - t0:.1f}s", res.error or res.stderr[:200]))
    else:
        out.append(Check("sandbox", "コード実行サンドボックス", WARN, "無効", advice="python.wasm をインストールしてください"))
    try:
        t0 = time.time()
        page = await p.web.fetch_text("https://example.com/", 2000)
        out.append(Check("web", "Web アクセス", PASS if page["status"] < 400 else WARN,
                         f"HTTP {page['status']} {time.time() - t0:.1f}s"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("web", "Web アクセス", WARN, "インターネットに接続できません", str(e)[:200]))
    return out


async def benchmarks(p: Any, job: Any, user: dict) -> list[Check]:
    from .models.manager import HOT
    from .profile.engine import Profile
    from .runners.common import gpu_lease, llm_call

    out: list[Check] = []
    llms = p.models.usable_models(("llm",))
    if not llms:
        return [Check("model_load", "モデルロード", FAIL, "利用可能なLLMがありません", advice="モデルをインストールしてください")]
    fast = next((s for s in llms if "fast" in s.roles), llms[0])
    general = next((s for s in llms if "general" in s.roles and s.id != fast.id), None)

    def prof(model_id: str) -> Profile:
        return Profile(task_type="chat", tuning=0.1, label="Speed", complexity=0.1, model_id=model_id, max_tokens=160,
                       temperature=0.2, priority_class="batch")

    async def measure_load(spec) -> tuple[float, int, int]:
        rt = p.models.runtimes[spec.id]
        if rt.state == HOT and rt.in_use == 0:
            await p.models.unload(spec.id, "benchmark")
        await asyncio.sleep(1.5)
        before = (await asyncio.to_thread(p.monitor.sample)).gpu
        t0 = time.time()
        async with gpu_lease(p, job, user, prof(spec.id), spec.kind, spec.id, 30):
            dt = time.time() - t0
            await asyncio.sleep(1.5)
            after = (await asyncio.to_thread(p.monitor.sample)).gpu
        used = (after.vram_used_mb - before.vram_used_mb) if (after and before) else 0
        predicted = rt.plan.est_vram_mb if rt.plan else 0
        return dt, used, predicted

    try:
        job.emit("progress", value=0.1, message=f"{fast.display_name} ロード計測")
        dt, used, pred = await measure_load(fast)
        st = PASS if dt < 30 else WARN
        out.append(Check("model_load", "モデルロード時間", st, f"{fast.display_name}: {dt:.1f}s",
                         f"VRAM実測 +{used}MB / 予測 {pred}MB"))
        if used > 200 and pred > 0 and p.gpu.name != "mock":
            p.models.record_calibration(fast.id, vram_correction=round(max(0.7, min(1.6, used / pred)), 3),
                                        measured_vram_mb=used, measured_at=now())
        job.emit("progress", value=0.35, message="推論速度計測")
        t0 = time.time()
        res = await llm_call(p, job, user, prof(fast.id), [
            {"role": "user", "content": "日本の四季について、それぞれ一文ずつ説明してください。"}], stream=False, max_tokens=160)
        elapsed = time.time() - t0
        tps = float(res.timings.get("predicted_per_second") or 0) or (
            int(res.usage.get("completion_tokens", 0) or 0) / max(0.01, elapsed))
        pps = float(res.timings.get("prompt_per_second") or 0)
        out.append(Check("inference", "推論速度", PASS if tps >= 20 else WARN if tps >= 5 else FAIL,
                         f"生成 {tps:.1f} tok/s" + (f" / プロンプト {pps:.0f} tok/s" if pps else ""), fast.display_name))
        p.db.execute("INSERT INTO bench_results(ts, kind, model_id, data) VALUES (?,?,?,?)",
                     (now(), "inference", fast.id, dumps({"tps": tps, "pps": pps, "load_seconds": dt, "vram_mb": used})))
        p.models.record_calibration(fast.id, tokens_per_second=round(tps, 1), load_seconds=round(dt, 1))
        if general:
            job.emit("progress", value=0.6, message=f"モデルスワップ計測 ({general.display_name})")
            dt2, used2, pred2 = await measure_load(general)
            out.append(Check("model_swap", "モデルスワップ", PASS if dt2 < 60 else WARN,
                             f"{general.display_name}: {dt2:.1f}s", f"VRAM実測 +{used2}MB / 予測 {pred2}MB"))
            if used2 > 200 and pred2 > 0 and p.gpu.name != "mock":
                p.models.record_calibration(general.id, vram_correction=round(max(0.7, min(1.6, used2 / pred2)), 3),
                                            measured_vram_mb=used2, load_seconds=round(dt2, 1), measured_at=now())
            res2 = await llm_call(p, job, user, prof(general.id), [
                {"role": "user", "content": "1から10までの素数を列挙してください。"}], stream=False, max_tokens=80)
            tps2 = float(res2.timings.get("predicted_per_second") or 0)
            if tps2:
                out.append(Check("inference_general", "推論速度 (汎用MoE)", PASS if tps2 >= 12 else WARN,
                                 f"{tps2:.1f} tok/s", general.display_name))
                p.models.record_calibration(general.id, tokens_per_second=round(tps2, 1))
        else:
            out.append(Check("model_swap", "モデルスワップ", SKIP, "比較対象の汎用モデルがありません"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("benchmark", "ベンチマーク", FAIL, type(e).__name__, str(e)[:300]))
    g = (await asyncio.to_thread(p.monitor.sample)).gpu
    if g and g.temp_c is not None:
        r = p.settings.resources
        out.append(Check("gpu_temp_load", "負荷後GPU温度", PASS if g.temp_c < r.gpu_temp_warn_c else WARN, f"{g.temp_c:.0f}°C"))
    return out


def summarize(checks: list[Check]) -> dict:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    overall = FAIL if counts[FAIL] else WARN if counts[WARN] else PASS
    return {"overall": overall, "counts": counts, "checks": [asdict(c) for c in checks], "ts": now()}


async def run_full(p: Any, job: Any, full: bool) -> dict:
    user = p.auth.get_user(job.user_id)
    job.emit("progress", value=0.02, message="システム診断")
    checks = await asyncio.to_thread(system_checks, p.settings)
    checks += await service_checks(p)
    if full:
        checks += await benchmarks(p, job, user)
    report = summarize(checks)
    report["full"] = full
    (p.settings.paths.diagnostics / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    job.emit("progress", value=1.0, message=report["overall"])
    return report
