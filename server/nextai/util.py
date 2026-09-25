from __future__ import annotations

import datetime as _dt
import json
import time
import uuid
from typing import Any


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: str | bytes | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def day_key(ts: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3000 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0xAC00 <= o <= 0xD7AF


def estimate_tokens(text: str) -> int:
    """Rough token estimate that works for mixed Japanese/English text."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return int(cjk * 0.9 + (len(text) - cjk) / 3.6) + 1


def truncate(text: str, limit: int, marker: str = "\n…(truncated)") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"
