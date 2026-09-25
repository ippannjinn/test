"""Per-user long-term memory with embedding search and a lexical fallback."""
from __future__ import annotations

import math
import re
import struct
from typing import Awaitable, Callable

from ..db import Database
from ..util import new_id, now

Embedder = Callable[[list[str]], Awaitable[tuple[str, list[list[float]]] | None]]
MAX_MEMORIES = 2000


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def _unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def _cos(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _grams(text: str) -> set[str]:
    t = re.sub(r"\s+", " ", text.lower())
    words = set(re.findall(r"[a-z0-9_]{3,}", t))
    cjk = re.sub(r"[^぀-ヿ一-鿿]", "", t)
    return words | {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def lexical_score(q: str, doc: str) -> float:
    a, b = _grams(q), _grams(doc)
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


class MemoryStore:
    def __init__(self, db: Database, embedder: Embedder | None = None):
        self.db = db
        self.embedder = embedder

    async def add(self, user_id: str, content: str, source: str = "user", pinned: bool = False) -> dict:
        content = (content or "").strip()[:2000]
        if not content:
            raise ValueError("empty memory")
        count = int(self.db.scalar("SELECT COUNT(*) FROM memories WHERE user_id=?", (user_id,)) or 0)
        if count >= MAX_MEMORIES:
            self.db.execute("DELETE FROM memories WHERE id IN (SELECT id FROM memories WHERE user_id=? AND pinned=0"
                            " ORDER BY updated_at ASC LIMIT 1)", (user_id,))
        emb, model = None, None
        if self.embedder:
            res = await self.embedder([content])
            if res:
                model, vecs = res
                emb = _pack(vecs[0])
        mid, ts = new_id(), now()
        self.db.execute("INSERT INTO memories(id, user_id, content, embedding, embed_model, source, pinned, created_at,"
                        " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (mid, user_id, content, emb, model, source, 1 if pinned else 0, ts, ts))
        return self.get(user_id, mid)

    def get(self, user_id: str, mem_id: str) -> dict | None:
        return self.db.one("SELECT id, content, source, pinned, created_at, updated_at FROM memories WHERE id=? AND user_id=?",
                           (mem_id, user_id))

    def list(self, user_id: str) -> list[dict]:
        return self.db.query("SELECT id, content, source, pinned, created_at, updated_at FROM memories WHERE user_id=?"
                             " ORDER BY pinned DESC, updated_at DESC", (user_id,))

    async def update(self, user_id: str, mem_id: str, content: str | None = None, pinned: bool | None = None) -> dict | None:
        row = self.get(user_id, mem_id)
        if not row:
            return None
        if content is not None:
            content = content.strip()[:2000]
            emb, model = None, None
            if self.embedder:
                res = await self.embedder([content])
                if res:
                    model, vecs = res
                    emb = _pack(vecs[0])
            self.db.execute("UPDATE memories SET content=?, embedding=?, embed_model=?, updated_at=? WHERE id=? AND user_id=?",
                            (content, emb, model, now(), mem_id, user_id))
        if pinned is not None:
            self.db.execute("UPDATE memories SET pinned=?, updated_at=? WHERE id=? AND user_id=?",
                            (1 if pinned else 0, now(), mem_id, user_id))
        return self.get(user_id, mem_id)

    def delete(self, user_id: str, mem_id: str) -> bool:
        return self.db.execute("DELETE FROM memories WHERE id=? AND user_id=?", (mem_id, user_id)).rowcount > 0

    async def search(self, user_id: str, query: str, k: int = 5, min_score: float = 0.12) -> list[dict]:
        rows = self.db.query("SELECT id, content, embedding, embed_model, pinned, updated_at FROM memories WHERE user_id=?",
                             (user_id,))
        if not rows or not query.strip():
            return []
        qvec, qmodel = None, None
        if self.embedder and any(r["embedding"] for r in rows):
            res = await self.embedder([query])
            if res:
                qmodel, vecs = res
                qvec = vecs[0]
        scored = []
        for r in rows:
            if qvec is not None and r["embedding"] and r["embed_model"] == qmodel:
                s = _cos(qvec, _unpack(r["embedding"]))
            else:
                s = lexical_score(query, r["content"])
            if r["pinned"]:
                s += 0.05
            if s >= min_score:
                scored.append((s, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"id": r["id"], "content": r["content"], "score": round(s, 3)} for s, r in scored[:k]]
