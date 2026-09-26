"""Layered settings: built-in defaults < config.toml < admin overrides stored in the DB."""
from __future__ import annotations

import copy
import json
import os
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "name": "NextAI Platform",
        "host": "0.0.0.0",
        "port": 8443,
        "tls": True,
        "cert_file": "",
        "key_file": "",
        "public_url": "",
        "tunnel_port": 8444,
        "cookie_secure": True,
        "allow_remote_admin": False,
        "trusted_proxies": [],
        "max_upload_mb": 50,
        "max_json_kb": 2048,
        "login_message": "",
        "update_manifest_url": "https://github.com/ippannjinn/test/releases/latest/download/update-manifest.json",
    },
    "auth": {
        "session_idle_minutes": 720,
        "session_absolute_hours": 168,
        "admin_session_idle_minutes": 480,
        "device_days": 90,
        "device_max_days": 365,
        "device_rotation_grace_seconds": 60,
        "password_min_length": 10,
        "login_max_failures": 5,
        "lockout_base_seconds": 60,
        "lockout_max_seconds": 3600,
        "ip_max_failures": 30,
        "api_rate_per_second": 8.0,
        "api_burst": 60,
        "disabled_retention_days": 30,
    },
    "resources": {
        "monitor_interval_seconds": 2.0,
        "vram_reserve_mb": 1024,
        "host_friendly": True,
        "ram_elevated_mb": 6144,
        "ram_high_mb": 4096,
        "ram_critical_mb": 2560,
        "ram_hysteresis_mb": 768,
        "disk_margin_gb": 15.0,
        "disk_low_buffer_gb": 5.0,
        "gpu_temp_warn_c": 83,
        "gpu_temp_pause_c": 88,
        "cpu_high_percent": 92,
        "gpu_provider": "auto",
    },
    "scheduler": {
        "max_wait_force_seconds": 180,
        "aging_per_second": 1.0,
        "swap_patience_seconds": 20,
        "fair_share_window_seconds": 600,
        "fair_share_penalty": 40,
        "hot_model_bonus": 25,
        "queue_timeout_seconds": 1800,
        "job_timeout_seconds": 3600,
        "jobs_per_minute": 20,
        "max_queued_per_user": 8,
        "tick_seconds": 1.0,
    },
    "models": {
        "backend_mode": "auto",
        "strategy": "single",
        "primary_model": "",
        "resident_fast_model": True,
        "idle_unload_seconds": 900,
        "prefetch": True,
        "thrash_window_seconds": 600,
        "thrash_max_swaps": 6,
        "kv_cache_type": "q8_0",
        "moe_ram_overcommit": 0.0,
        "cache_reuse": 256,
        "llm_parallel": 4,
        "base_port": 18080,
        "load_timeout_seconds": 600,
        "hf_endpoint": "https://huggingface.co",
        "hf_token": "",
        "cpu_threads": 0,
    },
    "profile": {
        "speed": {"max_tokens": 1024, "ctx_tokens": 4096, "max_steps": 2, "max_seconds": 90,
                  "max_tool_calls": 2, "tool_parallelism": 1, "temperature": 0.6},
        "balanced": {"max_tokens": 2048, "ctx_tokens": 12288, "max_steps": 6, "max_seconds": 420,
                     "max_tool_calls": 8, "tool_parallelism": 2, "temperature": 0.7},
        "autonomous": {"max_tokens": 6144, "ctx_tokens": 24576, "max_steps": 14, "max_seconds": 1500,
                       "max_tool_calls": 30, "tool_parallelism": 3, "temperature": 0.6},
        "max_consecutive_failures": 3,
        "max_total_tokens": 60000,
        "congestion_shift": 0.3,
        "idle_boost": 0.1,
        "speed_below": 0.34,
        "autonomous_above": 0.67,
        "reasoning_above": 0.55,
        "verify_above": 0.7,
    },
    "sandbox": {
        "backend": "auto",
        "timeout_seconds": 30,
        "memory_mb": 512,
        "disk_mb": 500,
        "max_output_kb": 64,
        "max_concurrent": 2,
        "full_python": True,
        "python_packages": ["numpy", "pandas", "matplotlib", "scipy", "scikit-learn", "pillow", "sympy", "networkx",
                            "statsmodels", "beautifulsoup4", "lxml", "xlrd", "pyyaml", "regex"],
    },
    "tools": {
        "auto_install": True,
        "allowed": ["ffmpeg", "pandoc"],
        "timeout_seconds": 300,
    },
    "web": {
        "search_provider": "duckduckgo",
        "searxng_url": "",
        "brave_api_key": "",
        "timeout_seconds": 15,
        "max_bytes": 3000000,
        "max_download_mb": 50,
        "max_requests_per_job": 24,
        "max_redirects": 4,
        "allowed_ports": [80, 443, 8080, 8443],
        "use_system_proxy": False,
        "user_agent": "Mozilla/5.0 (compatible; NextAI-Platform/1.0; +private)",
    },
    "generation": {
        "image_max_side": 1024,
        "image_max_steps": 8,
        "video_max_frames": 49,
        "video_max_side": 832,
        "video_timeout_seconds": 1800,
        "music_max_seconds": 30,
        "image_cost": 1,
        "video_cost": 10,
        "music_cost": 3,
    },
    "storage": {
        "default_quota_mb": 5120,
        "tmp_max_age_hours": 24,
        "keep_backups": 7,
    },
    "api": {
        "enabled": True,
        "member_keys": True,
        "key_max_days": 365,
        "max_keys_per_user": 10,
        "max_tokens_cap": 8192,
    },
    "users": {
        "default_generation_quota": 100,
        "default_concurrent_jobs": 2,
        "default_rate_limit_per_min": 30,
    },
}

RESTART_KEYS_PREFIXES = ("server.", "models.backend_mode", "models.base_port", "resources.gpu_provider")
SECRET_KEYS = {"web.brave_api_key", "models.hf_token"}


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


FLAT_DEFAULTS = _flatten(DEFAULTS)


def _coerce(key: str, value: Any) -> Any:
    default = FLAT_DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else [x.strip() for x in value.split(",") if x.strip()]
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        if default and isinstance(default[0], int):
            return [int(x) for x in value]
        return [str(x) for x in value]
    return str(value)


class _Section:
    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        try:
            v = self._data[name]
        except KeyError as e:
            raise AttributeError(name) from e
        return _Section(v) if isinstance(v, dict) else v

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


@dataclass
class Paths:
    data_dir: Path

    @property
    def db(self) -> Path: return self.data_dir / "nextai.db"
    @property
    def users(self) -> Path: return self.data_dir / "users"
    @property
    def models(self) -> Path: return self.data_dir / "models"
    @property
    def runtime(self) -> Path: return self.data_dir / "runtime"
    @property
    def logs(self) -> Path: return self.data_dir / "logs"
    @property
    def certs(self) -> Path: return self.data_dir / "certs"
    @property
    def backups(self) -> Path: return self.data_dir / "backups"
    @property
    def tmp(self) -> Path: return self.data_dir / "tmp"
    @property
    def diagnostics(self) -> Path: return self.data_dir / "diagnostics"
    @property
    def config_file(self) -> Path: return self.data_dir / "config.toml"

    def ensure(self) -> None:
        for p in (self.data_dir, self.users, self.models, self.runtime, self.logs, self.certs,
                  self.backups, self.tmp, self.diagnostics):
            p.mkdir(parents=True, exist_ok=True)


class Settings:
    def __init__(self, data_dir: Path, file_values: dict[str, Any] | None = None):
        self.paths = Paths(Path(data_dir).resolve())
        self._file = _flatten(file_values or {})
        self._overrides: dict[str, Any] = {}
        self.unknown_keys = sorted(k for k in self._file if k not in FLAT_DEFAULTS and not k.startswith("paths."))
        if self.unknown_keys:
            # Tolerated so that config files written by other versions never block startup after an update.
            import logging

            logging.getLogger("nextai.config").warning("ignoring unknown config keys: %s", ", ".join(self.unknown_keys))
        self._file = {k: v for k, v in self._file.items() if k in FLAT_DEFAULTS}
        bad = []
        for k, v in list(self._file.items()):
            try:
                _coerce(k, v)
            except (TypeError, ValueError):
                bad.append(k)
                del self._file[k]
        if bad:
            import logging

            logging.getLogger("nextai.config").warning("ignoring invalid config values: %s", ", ".join(bad))
        self._rebuild()

    def _rebuild(self) -> None:
        merged = copy.deepcopy(DEFAULTS)
        for layer in (self._file, self._overrides):
            for key, value in layer.items():
                if key not in FLAT_DEFAULTS:
                    continue
                cur = merged
                parts = key.split(".")
                for p in parts[:-1]:
                    cur = cur[p]
                cur[parts[-1]] = _coerce(key, value)
        self._merged = merged

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        merged = self.__dict__.get("_merged")
        if merged is None or name not in merged:
            raise AttributeError(name)
        return _Section(merged[name])

    def get(self, key: str) -> Any:
        cur: Any = self._merged
        for p in key.split("."):
            cur = cur[p]
        return copy.deepcopy(cur)

    def load_overrides(self, rows: dict[str, Any]) -> None:
        self._overrides = {k: v for k, v in rows.items() if k in FLAT_DEFAULTS}
        self._rebuild()

    def validate_override(self, key: str, value: Any) -> Any:
        if key not in FLAT_DEFAULTS:
            raise KeyError(key)
        return _coerce(key, value)

    def set_override(self, key: str, value: Any) -> Any:
        coerced = self.validate_override(key, value)
        self._overrides[key] = coerced
        self._rebuild()
        return coerced

    def clear_override(self, key: str) -> None:
        self._overrides.pop(key, None)
        self._rebuild()

    def describe(self) -> list[dict[str, Any]]:
        flat = _flatten(self._merged)
        out = []
        for key, default in FLAT_DEFAULTS.items():
            value = flat[key]
            secret = key in SECRET_KEYS
            out.append({
                "key": key,
                "section": key.split(".")[0],
                "value": ("********" if value else "") if secret else value,
                "default": "" if secret else default,
                "type": type(default).__name__,
                "source": "admin" if key in self._overrides else "file" if key in self._file else "default",
                "restart_required": key.startswith(RESTART_KEYS_PREFIXES),
                "secret": secret,
            })
        return out


def _read_config(path: Path) -> dict:
    """Security software can briefly lock files it is scanning (Windows reports that as permission denied)."""
    if not path.exists():
        return {}
    for attempt in range(6):
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.5 * (attempt + 1))
    return {}


def load_settings(data_dir: str | Path | None = None, config_file: str | Path | None = None) -> Settings:
    if config_file is not None:
        cfg_path = Path(config_file)
        values = _read_config(cfg_path)
        data_dir = data_dir or values.get("paths", {}).get("data_dir") or cfg_path.parent
    else:
        data_dir = data_dir or os.environ.get("NEXTAI_DATA_DIR") or _default_data_dir()
        cfg_path = Path(data_dir) / "config.toml"
        values = _read_config(cfg_path)
    values.pop("paths", None)
    return Settings(Path(data_dir), values)


def _default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "NextAI"
    return Path.home() / ".local" / "share" / "nextai"


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def write_config(path: Path, values: dict[str, dict[str, Any]]) -> None:
    """Write a minimal TOML config containing only the provided (non-default) values."""
    lines = ["# NextAI Platform configuration. Values here override built-in defaults.",
             "# Most settings can also be changed from the Admin desktop app.", ""]
    for section, kv in values.items():
        lines.append(f"[{section}]")
        for k, v in kv.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
