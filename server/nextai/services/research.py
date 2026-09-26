"""Web research engine (Perplexity / ChatGPT-search style retrieval-augmented answering).

question → several search queries → metasearch (parallel, rank-fused) → read the best pages in parallel
(SSRF-safe) → split into passages → rank passages against the question (lexical + optional embeddings)
→ a compact, numbered evidence pack the model answers from with [n] citations.

Small local models are bad at orchestrating many search/fetch tool calls, so this runs as ONE step: either
automatically before the model answers (auto-research) or as the `web_research` tool.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..security.ssrf import SSRFError
from .memory import _cos, lexical_score

log = logging.getLogger("nextai.research")

# polite / filler endings that make poor search queries
_FILLER = re.compile(r"(について|を)?(詳しく)?(教えて(ください|下さい)?|知りたい(です)?|調べて(ください)?|って何(ですか)?|ってなに|"
                     r"とは(何|なに)?(ですか)?|は何(ですか)?|はなに|ですか|でしょうか|ください|下さい|[?？!！。、\s]+)$")
_LOOKUP = re.compile(r"(とは|って(何|なに|誰|だれ)|は(何|誰|だれ|どこ|いつ)|何者|について(教えて|知りたい|調べて)|教えて|"
                     r"最新|ニュース|発売|リリース|価格|値段|評判|口コミ|攻略|キャラ|何年|いつから|どこで|"
                     r"\b(what is|who is|when|where|latest|news|release|price)\b)", re.I)
_PROPER = re.compile(r"[ァ-ヴー]{3,}|[A-Z][a-zA-Z0-9]{2,}|「[^」]{2,30}」|『[^』]{2,30}』")


def looks_like_lookup(text: str) -> bool:
    """A question about something in the world (an entity, a fact, current info) that search can answer."""
    t = text.strip()
    if len(t) < 3 or len(t) > 400:
        return False
    return bool(_LOOKUP.search(t)) and (bool(_PROPER.search(t)) or bool(re.search(r"最新|ニュース|今日|今年|現在", t)))


def make_queries(question: str, extra: list[str] | None = None, limit: int = 3) -> list[str]:
    q = re.sub(r"\s+", " ", question).strip()
    core = _FILLER.sub("", q).strip(" 「」『』") or q
    out = [core]
    ents = [e.strip("「」『』") for e in _PROPER.findall(q)]
    if ents:
        main = max(ents, key=len)
        if main != core:
            out.append(main)
        out.append(f"{main} とは" if re.search(r"[ァ-ヴー一-鿿]", main) else f"{main} wiki")
    out += [e for e in (extra or []) if e]
    seen, res = set(), []
    for x in out:
        k = x.lower()
        if x and k not in seen:
            seen.add(k)
            res.append(x[:200])
    return res[:limit + len(extra or [])]


@dataclass
class Source:
    n: int
    title: str
    url: str
    passages: list[str] = field(default_factory=list)


@dataclass
class ResearchResult:
    question: str
    queries: list[str]
    sources: list[Source]
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return any(s.passages for s in self.sources)

    def evidence(self, limit_chars: int = 9000) -> str:
        parts, used = [], 0
        for s in self.sources:
            if not s.passages:
                continue
            block = f"[{s.n}] {s.title}\nURL: {s.url}\n" + "\n…\n".join(s.passages)
            if used + len(block) > limit_chars:
                block = block[: max(0, limit_chars - used)]
            parts.append(block)
            used += len(block)
            if used >= limit_chars:
                break
        return "\n\n".join(parts)

    def source_list(self) -> list[dict]:
        return [{"n": s.n, "title": s.title, "url": s.url} for s in self.sources if s.passages]


def _passages(text: str, size: int = 700) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) > 30]
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) > size and buf:
            out.append(buf)
            buf = ""
        buf = (buf + "\n" + p).strip() if buf else p[:size * 2]
    if buf:
        out.append(buf)
    return out[:80]


class Researcher:
    def __init__(self, platform: Any):
        self.p = platform

    async def run(self, question: str, *, queries: list[str] | None = None, max_pages: int = 5, per_page: int = 3,
                  emit: Callable[..., None] | None = None, budget: Callable[[], None] | None = None) -> ResearchResult:
        t0 = time.time()
        web = self.p.web
        qs = make_queries(question, queries)
        say = emit or (lambda *a, **k: None)
        say("research", phase="search", queries=qs)
        hits_lists = await asyncio.gather(*(web.search(q, 8) for q in qs), return_exceptions=True)
        fused: dict[str, dict] = {}
        for hits in hits_lists:
            if isinstance(hits, BaseException):
                log.info("search failed: %s", hits)
                continue
            for rank, h in enumerate(hits):
                cur = fused.setdefault(h["url"], {**h, "score": 0.0})
                cur["score"] += 1.0 / (60 + rank) + 0.002 * lexical_score(question, h["title"] + " " + h["snippet"])
        ranked = sorted(fused.values(), key=lambda x: -x["score"])
        say("research", phase="read", pages=[h["url"] for h in ranked[:max_pages]])

        async def read(h: dict) -> tuple[dict, str]:
            try:
                if budget:
                    budget()
                page = await asyncio.wait_for(web.fetch_text(h["url"], 40000), 12)
                return h, (page.get("text") or "") if page.get("status", 500) < 400 else ""
            except (SSRFError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - a bad page is just skipped
                log.info("research fetch failed %s: %s", h["url"], e)
                return h, ""

        pages = await asyncio.gather(*(read(h) for h in ranked[:max_pages]))
        # candidate passages: page chunks + search snippets (snippets keep answers even when a page can't be read)
        cands: list[tuple[int, str]] = []
        for i, (h, text) in enumerate(pages):
            for chunk in _passages(text):
                cands.append((i, chunk))
            if h.get("snippet"):
                cands.append((i, h["snippet"]))
        for j, h in enumerate(ranked[max_pages:max_pages + 5]):
            if h.get("snippet"):
                cands.append((max_pages + j, h["snippet"]))
        scored = await self._score(question, cands)
        by_src: dict[int, list[str]] = {}
        for score, (i, chunk) in scored:
            if score <= 0 or len(by_src.get(i, [])) >= per_page:
                continue
            by_src.setdefault(i, []).append(chunk.strip()[:900])
        all_hits = [h for h, _ in pages] + ranked[max_pages:max_pages + 5]
        sources, n = [], 0
        for i, h in enumerate(all_hits):
            if i in by_src:
                n += 1
                sources.append(Source(n, h.get("title") or h["url"], h["url"], by_src[i]))
        res = ResearchResult(question, qs, sources, round(time.time() - t0, 2))
        say("research", phase="done", sources=len(res.source_list()), seconds=res.elapsed)
        return res

    async def _score(self, question: str, cands: list[tuple[int, str]]) -> list[tuple[float, tuple[int, str]]]:
        lex = [lexical_score(question, c) for _, c in cands]
        sem = [0.0] * len(cands)
        if cands:
            try:
                emb = await asyncio.wait_for(self.p.embed([question] + [c for _, c in cands[:120]]), 20)
            except Exception:  # noqa: BLE001 - lexical ranking still works
                emb = None
            if emb:
                _, vecs = emb
                q = vecs[0]
                for k, v in enumerate(vecs[1:]):
                    sem[k] = max(0.0, _cos(q, v))
        out = [(0.6 * lex[k] + 0.4 * sem[k] + (0.05 if k < 3 else 0.0), cands[k]) for k in range(len(cands))]
        return sorted(out, key=lambda x: -x[0])
