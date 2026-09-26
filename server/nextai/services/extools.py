"""External command-line tools (ffmpeg, pandoc) that are downloaded the first time they are needed.

They run on the host (native code can't run in the WASM sandbox), so they are only ever called with argument
lists built by NextAI itself - never a shell, never user-supplied flags - on files inside a sandbox workspace,
with a timeout and below-normal priority.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..backends.base import find_executable, no_window_flags
from ..install.downloader import ensure_space
from ..install.runtime import EXTERNAL_TOOLS, RuntimeInstaller

log = logging.getLogger("nextai.extools")


class ToolUnavailable(RuntimeError):
    pass


class ExternalTools:
    def __init__(self, platform: Any):
        self.p = platform
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def rt(self) -> Path:
        return self.p.settings.paths.runtime

    def path(self, name: str, exe: str | None = None) -> Path | None:
        if name not in EXTERNAL_TOOLS:
            return None
        main = find_executable(self.rt, name, [EXTERNAL_TOOLS[name]["exe"]])
        if main is None or exe is None:
            return main
        sibling = main.with_name(exe + (".exe" if os.name == "nt" else ""))
        return sibling if sibling.exists() else None

    def status(self) -> list[dict]:
        out = []
        for name, spec in EXTERNAL_TOOLS.items():
            p = self.path(name)
            out.append({"name": name, "installed": bool(p), "purpose": spec["purpose"], "license": spec["license"],
                        "allowed": name in self.p.settings.tools.allowed})
        return out

    async def ensure(self, name: str, emit: Callable[..., None] | None = None) -> Path:
        """Path of the tool, installing it first if needed (and allowed)."""
        t = self.p.settings.tools
        if name not in EXTERNAL_TOOLS or name not in t.allowed:
            raise ToolUnavailable(f"{name} は管理者により許可されていません")
        found = self.path(name)
        if found:
            return found
        if not t.auto_install:
            raise ToolUnavailable(f"{name} がインストールされていません (自動インストールは無効です。管理者に依頼してください)")
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            found = self.path(name)
            if found:
                return found
            say = emit or (lambda *a, **k: None)
            say("notice", message=f"{name} を初回のみダウンロードしています (約{EXTERNAL_TOOLS[name]['size_mb']}MB)…")
            ensure_space(self.rt, EXTERNAL_TOOLS[name]["size_mb"] * 2**20 * 3, self.p.settings.resources.disk_margin_gb)

            def progress(ev: dict) -> None:
                if ev.get("event") == "progress" and ev.get("size"):
                    say("progress", value=round(ev["done"] / ev["size"], 3), message=f"{name} をダウンロード中")

            try:
                await asyncio.to_thread(lambda: RuntimeInstaller(self.p.settings, progress).install_tool(name))
            except Exception as e:  # noqa: BLE001
                log.warning("installing %s failed: %s", name, e)
                raise ToolUnavailable(f"{name} をダウンロードできませんでした: {e}") from e
            found = self.path(name)
            if not found:
                raise ToolUnavailable(f"{name} のインストールに失敗しました")
            log.info("installed external tool %s", name)
            return found

    async def run(self, exe: Path, args: list[str], cwd: Path, timeout: float | None = None) -> tuple[int, str]:
        timeout = timeout or float(self.p.settings.tools.timeout_seconds)
        env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")}
        proc = await asyncio.create_subprocess_exec(str(exe), *args, cwd=str(cwd), env=env,
                                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                    stderr=subprocess.STDOUT, **no_window_flags(True))
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, out.decode("utf-8", "replace")[-4000:]
