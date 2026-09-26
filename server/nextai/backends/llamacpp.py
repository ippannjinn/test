"""llama.cpp `llama-server` process management + OpenAI-compatible client."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import psutil

from ..config import Settings
from ..models.catalog import ModelSpec
from ..models.planner import LaunchPlan
from ..security.tokens import new_token
from .base import BackendError, ChatEvent, ChatRequest, LLMBackend, find_executable, free_port, lower_priority, no_window_flags

log = logging.getLogger("nextai.llamacpp")


@dataclass
class LlamaInstance:
    proc: subprocess.Popen
    port: int
    base_url: str
    log_path: Path
    started_at: float


class LlamaCppBackend(LLMBackend):
    name = "llamacpp"

    def __init__(self, settings: Settings, runtime_dir: Path, log_dir: Path):
        self.settings = settings
        self.log_dir = log_dir
        self.exe = find_executable(runtime_dir, "llama.cpp", ["llama-server"])
        self.available = self.exe is not None
        self.api_key = new_token(18)
        self._help: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=60, pool=30), trust_env=False)

    def reload(self, runtime_dir: Path) -> None:
        """Pick up a newly installed llama.cpp (it goes into a new versioned folder, so running servers are
        untouched; only models loaded from now on use it)."""
        exe = find_executable(runtime_dir, "llama.cpp", ["llama-server"])
        if exe is not None:
            self.exe, self.available, self._help = exe, True, None

    def help_text(self) -> str:
        if self._help is None:
            try:
                r = subprocess.run([str(self.exe), "--help"], capture_output=True, text=True, timeout=30,
                                   errors="replace", **no_window_flags(False))
                self._help = (r.stdout or "") + (r.stderr or "")
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("llama-server --help failed: %s", e)
                self._help = ""
        return self._help

    def has_flag(self, flag: str) -> bool:
        return re.search(rf"(^|[\s,]){re.escape(flag)}([\s,=]|$)", self.help_text(), re.M) is not None

    def build_args(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan, port: int) -> list[str]:
        m = self.settings.models
        args = [str(self.exe), "-m", str(paths["model"]), "--host", "127.0.0.1", "--port", str(port),
                "-c", str(plan.ctx), "-np", str(plan.parallel), "-ngl", str(plan.n_gpu_layers if plan.gpu else 0),
                "--api-key", self.api_key]
        threads = m.cpu_threads or max(2, (psutil.cpu_count(logical=False) or 4) - 2)
        args += ["-t", str(threads)]
        if spec.kind == "embedding":
            args.append("--embeddings" if self.has_flag("--embeddings") else "--embedding")
            return args
        args.append("--jinja")
        if self.has_flag("--metrics"):
            args.append("--metrics")
        if self.has_flag("--no-webui"):
            args.append("--no-webui")
        if plan.gpu:
            fa_new = re.search(r"--flash-attn\s+\[?on\|off\|auto", self.help_text()) is not None
            args += ["-fa", "on"] if fa_new else ["-fa"]
            if plan.kv_type and plan.kv_type != "f16":
                args += ["--cache-type-k", plan.kv_type, "--cache-type-v", plan.kv_type]
        if plan.n_cpu_moe > 0:
            if self.has_flag("--n-cpu-moe"):
                args += ["--n-cpu-moe", str(plan.n_cpu_moe)]
            else:
                layers = "|".join(str(i) for i in range(plan.n_cpu_moe))
                args += ["-ot", rf"blk\.({layers})\.ffn_.*_exps\.=CPU"]
        if m.cache_reuse and self.has_flag("--cache-reuse"):
            args += ["--cache-reuse", str(m.cache_reuse)]
        if plan.parallel > 1 and self.has_flag("--kv-unified"):
            args.append("--kv-unified")
        if "mmproj" in paths:
            args += ["--mmproj", str(paths["mmproj"])]
        return args

    async def start(self, spec: ModelSpec, paths: dict[str, Path], plan: LaunchPlan) -> LlamaInstance:
        if not self.available:
            raise BackendError("llama.cpp ランタイムがインストールされていません")
        for comp, p in paths.items():
            if not p.exists():
                raise BackendError(f"モデルファイルがありません: {comp}")
        port = free_port()
        args = self.build_args(spec, paths, plan, port)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"llama-{spec.id}.log"
        logf = open(log_path, "ab")
        logf.write(f"\n==== {time.ctime()} {' '.join(a if a != self.api_key else '***' for a in args)}\n".encode())
        logf.flush()
        env = dict(os.environ)
        proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                cwd=str(Path(self.exe).parent), **no_window_flags())
        logf.close()
        lower_priority(proc.pid)
        inst = LlamaInstance(proc, port, f"http://127.0.0.1:{port}", log_path, time.time())
        deadline = time.time() + self.settings.models.load_timeout_seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                raise BackendError(f"llama-server が起動に失敗しました (exit {proc.returncode})。ログ: {log_path}\n"
                                   + _tail(log_path))
            try:
                r = await self._client.get(inst.base_url + "/health", headers=self._headers(), timeout=5)
                if r.status_code == 200:
                    return inst
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        await self.stop(inst)
        raise BackendError("llama-server の起動がタイムアウトしました")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def stop(self, instance: LlamaInstance) -> None:
        if instance is None:
            return
        proc = instance.proc
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 15)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait, 10)

    def alive(self, instance: LlamaInstance) -> bool:
        return instance is not None and instance.proc.poll() is None

    def _payload(self, spec: ModelSpec, req: ChatRequest) -> dict[str, Any]:
        p: dict[str, Any] = {
            "messages": req.messages, "stream": True, "max_tokens": req.max_tokens,
            "temperature": req.temperature, "top_p": req.top_p, "cache_prompt": True,
            "stream_options": {"include_usage": True},
        }
        if req.stop:
            p["stop"] = req.stop
        if req.tools:
            p["tools"] = req.tools
            p["tool_choice"] = "auto"
        if req.json_mode:
            p["response_format"] = {"type": "json_object"}
        if spec.reasoning_control == "effort":
            effort = {"off": "low", "low": "low", "medium": "medium", "high": "high"}[req.reasoning]
            p["reasoning_effort"] = effort
            p["chat_template_kwargs"] = {"reasoning_effort": effort}
        elif spec.reasoning_control == "qwen3_toggle":
            p["chat_template_kwargs"] = {"enable_thinking": req.reasoning != "off"}
        return p

    async def chat(self, instance: LlamaInstance, spec: ModelSpec, req: ChatRequest) -> AsyncIterator[ChatEvent]:
        calls: dict[int, dict[str, str]] = {}
        finish, usage, timings = None, {}, {}
        try:
            async with self._client.stream("POST", instance.base_url + "/v1/chat/completions",
                                           json=self._payload(spec, req), headers=self._headers()) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")[:500]
                    raise BackendError(f"推論エラー HTTP {r.status_code}: {body}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    if obj.get("timings"):
                        timings = obj["timings"]
                    for ch in obj.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("reasoning_content"):
                            yield ChatEvent("reasoning", text=delta["reasoning_content"])
                        if delta.get("content"):
                            yield ChatEvent("content", text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            c = calls.setdefault(idx, {"id": tc.get("id") or f"call_{idx}", "name": "", "arguments": ""})
                            if tc.get("id"):
                                c["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                c["name"] = c["name"] + fn["name"] if not c["name"].endswith(fn["name"]) else c["name"]
                            if fn.get("arguments"):
                                c["arguments"] += fn["arguments"]
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        except httpx.HTTPError as e:
            raise BackendError(f"推論サーバーとの通信に失敗しました: {e}") from e
        if calls:
            yield ChatEvent("tool_calls", tool_calls=[calls[i] for i in sorted(calls)])
        yield ChatEvent("done", finish_reason=finish or ("tool_calls" if calls else "stop"), usage=usage, timings=timings)

    async def embed(self, instance: LlamaInstance, texts: list[str]) -> list[list[float]]:
        try:
            r = await self._client.post(instance.base_url + "/v1/embeddings", json={"input": texts},
                                        headers=self._headers(), timeout=120)
        except httpx.HTTPError as e:
            raise BackendError(f"埋め込みサーバーとの通信に失敗しました: {e}") from e
        if r.status_code != 200:
            raise BackendError(f"埋め込みエラー HTTP {r.status_code}")
        data = r.json().get("data", [])
        return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]


def _tail(path: Path, n: int = 15) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""
