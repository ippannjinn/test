"""Tool definitions exposed to the LLM (OpenAI function-calling schema)."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..security.ssrf import SSRFError
from ..services.memory import lexical_score


@dataclass
class ToolContext:
    platform: Any
    user: dict
    job: Any
    workspace: Path | None
    attachments: list[dict] = field(default_factory=list)
    web_requests: int = 0
    assets: list[dict] = field(default_factory=list)


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict = field(default_factory=dict)


class Tool:
    name = ""
    description = ""
    parameters: dict = {"type": "object", "properties": {}}
    timeout = 60.0

    async def run(self, ctx: ToolContext, args: dict) -> ToolResult:
        raise NotImplementedError

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def _web_budget(ctx: ToolContext) -> None:
    limit = ctx.platform.settings.web.max_requests_per_job
    if ctx.web_requests >= limit:
        raise ToolError(f"このタスクでのWebアクセス上限 ({limit}回) に達しました")
    ctx.web_requests += 1


class ToolError(Exception):
    pass


class WebSearch(Tool):
    name = "web_search"
    description = "Search the web. Returns titles, URLs and snippets. Use for current or unknown information."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "search query"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}
    timeout = 30

    async def run(self, ctx, args):
        _web_budget(ctx)
        results = await ctx.platform.web.search(str(args.get("query", "")), int(args.get("max_results", 6) or 6))
        if not results:
            return ToolResult(False, "検索結果がありませんでした")
        lines = [f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet'][:300]}" for i, r in enumerate(results)]
        return ToolResult(True, "\n".join(lines), {"results": results})


class WebFetch(Tool):
    name = "web_fetch"
    description = "Fetch a public web page (http/https) and return its readable text."
    parameters = {"type": "object", "properties": {
        "url": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 500, "maximum": 30000}},
        "required": ["url"]}
    timeout = 45

    async def run(self, ctx, args):
        _web_budget(ctx)
        try:
            page = await ctx.platform.web.fetch_text(str(args.get("url", "")), int(args.get("max_chars", 10000) or 10000))
        except SSRFError as e:
            return ToolResult(False, f"アクセスが拒否されました: {e}")
        links = "\n".join(f"- {t}: {u}" for t, u in page.get("links", [])[:10])
        return ToolResult(page["status"] < 400,
                          f"URL: {page['url']}\nHTTP {page['status']}\nTitle: {page['title']}\n\n{page['text']}"
                          + (f"\n\nLinks:\n{links}" if links else ""), {"url": page["url"]})


class RunCode(Tool):
    name = "run_code"
    description = ("Run Python 3 code in an isolated sandbox (no network, limited CPU/RAM/time). "
                   "Print results to stdout. Files written to the current directory are returned.")
    parameters = {"type": "object", "properties": {"code": {"type": "string", "description": "complete Python program"}},
                  "required": ["code"]}
    timeout = 90

    async def run(self, ctx, args):
        code = str(args.get("code", ""))
        if not code.strip():
            return ToolResult(False, "コードが空です")
        res = await ctx.platform.sandbox.run(ctx.user["id"], code, workspace=ctx.workspace)
        for name, data in list(res.artifacts.items())[:5]:
            try:
                row = ctx.platform.files.save_bytes(ctx.user, Path(name).name, data, kind="generated", job_id=ctx.job.id,
                                                    meta={"source": "sandbox"})
                ctx.assets.append(row)
            except Exception:  # noqa: BLE001 - quota errors are reported below
                res.error = (res.error + " / 出力ファイルを保存できませんでした").strip(" /")
        return ToolResult(res.ok, res.to_text(), {"exit_code": res.exit_code, "files": res.files})


def _chunks(text: str, size: int = 1800) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class ReadFile(Tool):
    name = "read_file"
    description = "Read text from one of the user's attached/uploaded files. Optionally focus on a query or offset."
    parameters = {"type": "object", "properties": {
        "file_id": {"type": "string"}, "name": {"type": "string"}, "query": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0}}, "required": []}

    async def run(self, ctx, args):
        fs = ctx.platform.files
        row = None
        if args.get("file_id"):
            row = fs.get(ctx.user["id"], str(args["file_id"]))
        elif args.get("name"):
            name = str(args["name"])
            row = next((fs.get(ctx.user["id"], a["id"]) for a in ctx.attachments if a.get("name") == name), None)
        elif ctx.attachments:
            row = fs.get(ctx.user["id"], ctx.attachments[0]["id"])
        if not row:
            names = ", ".join(f"{a['name']} ({a['id']})" for a in ctx.attachments) or "なし"
            return ToolResult(False, f"ファイルが見つかりません。利用可能: {names}")
        text = await asyncio.to_thread(fs.extract_text, row)
        if args.get("query"):
            chunks = _chunks(text)
            best = sorted(range(len(chunks)), key=lambda i: lexical_score(str(args["query"]), chunks[i]), reverse=True)[:4]
            body = "\n...\n".join(chunks[i] for i in sorted(best))
        else:
            off = int(args.get("offset", 0) or 0)
            body = text[off:off + 12000]
            if off + 12000 < len(text):
                body += f"\n…(続きあり: offset={off + 12000}, 全{len(text)}文字)"
        return ToolResult(True, f"[{row['name']}]\n{body}")


_SAFE_REL = re.compile(r"^(?!/)(?!.*\.\.)[\w\-. /]{1,200}$")


def _ws_path(ctx: ToolContext, rel: str) -> Path:
    if ctx.workspace is None:
        raise ToolError("ワークスペースがありません")
    rel = rel.strip().lstrip("./") if rel.strip().startswith("./") else rel.strip()
    if not _SAFE_REL.match(rel) or rel.startswith("__nextai"):
        raise ToolError("不正なパスです (相対パス、英数字のみ)")
    p = (ctx.workspace / rel).resolve()
    if not str(p).startswith(str(ctx.workspace.resolve())):
        raise ToolError("ワークスペース外へのアクセスは禁止されています")
    return p


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a file in the project workspace (relative path)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path", "content"]}

    async def run(self, ctx, args):
        content = str(args.get("content", ""))
        if len(content.encode("utf-8")) > 1_000_000:
            return ToolResult(False, "ファイルが大きすぎます (1MBまで)")
        p = _ws_path(ctx, str(args.get("path", "")))
        ctx.platform.files.check_quota(ctx.user, len(content.encode("utf-8")))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(True, f"wrote {p.relative_to(ctx.workspace).as_posix()} ({len(content)} chars)")


class ReadWorkspace(Tool):
    name = "read_workspace"
    description = "Read a file from the project workspace."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def run(self, ctx, args):
        p = _ws_path(ctx, str(args.get("path", "")))
        if not p.is_file():
            return ToolResult(False, "ファイルがありません")
        return ToolResult(True, p.read_text(encoding="utf-8", errors="replace")[:20000])


class ListWorkspace(Tool):
    name = "list_workspace"
    description = "List files in the project workspace."
    parameters = {"type": "object", "properties": {}}

    async def run(self, ctx, args):
        if ctx.workspace is None:
            return ToolResult(False, "ワークスペースがありません")
        files = [f"{p.relative_to(ctx.workspace).as_posix()} ({p.stat().st_size}B)"
                 for p in sorted(ctx.workspace.rglob("*")) if p.is_file()]
        return ToolResult(True, "\n".join(files) or "(空)")


class MemorySearch(Tool):
    name = "memory_search"
    description = "Search the user's long-term memory (facts and preferences they asked you to remember)."
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    async def run(self, ctx, args):
        hits = await ctx.platform.memory.search(ctx.user["id"], str(args.get("query", "")), k=6)
        return ToolResult(True, "\n".join(f"- {h['content']}" for h in hits) or "該当する記憶はありません")


class MemorySave(Tool):
    name = "memory_save"
    description = "Save a durable fact or preference about the user to long-term memory (only when clearly useful later)."
    parameters = {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}

    async def run(self, ctx, args):
        row = await ctx.platform.memory.add(ctx.user["id"], str(args.get("content", "")), source="agent")
        return ToolResult(True, f"記憶しました: {row['content'][:200]}")


_ASPECT = {"type": "string", "enum": ["square", "landscape", "portrait"],
           "description": "square (1:1), landscape (16:9-ish) or portrait (9:16-ish)"}


class GenerateMedia(Tool):
    """Image / video / music generation as a tool: the LLM writes the prompt and picks parameters, the platform
    runs the generator through the GPU scheduler (queue, fairness, VRAM swap) and shows the result to the user."""

    SPECS = {
        "image": ("generate_image", "画像",
                  "Generate an image with the local image model and show it to the user. Use whenever the user asks "
                  "for a picture, illustration, photo, icon, logo, wallpaper, etc. Write `prompt` in English as a "
                  "detailed visual description (subject, style, composition, lighting, colors).",
                  {"prompt": {"type": "string"}, "negative_prompt": {"type": "string", "description": "things to avoid"},
                   "aspect": _ASPECT, "seed": {"type": "integer"}}),
        "video": ("generate_video", "動画",
                  "Generate a short video clip (a few seconds, low resolution, local GPU) and show it to the user. "
                  "Write `prompt` in English describing the scene and the motion.",
                  {"prompt": {"type": "string"}, "negative_prompt": {"type": "string"}, "aspect": _ASPECT,
                   "seconds": {"type": "number", "minimum": 1, "maximum": 5}, "seed": {"type": "integer"}}),
        "music": ("generate_music", "音楽",
                  "Compose instrumental music / BGM (no vocals) with the local music model and give it to the user. "
                  "Write `prompt` in English: genre, mood, instruments, tempo.",
                  {"prompt": {"type": "string"}, "seconds": {"type": "number", "minimum": 2, "maximum": 30}}),
    }

    def __init__(self, kind: str):
        self.kind = kind
        self.name, self.label, self.description, props = self.SPECS[kind]
        self.parameters = {"type": "object", "properties": props, "required": ["prompt"]}
        self.timeout = 1900.0 if kind == "video" else 960.0

    async def run(self, ctx, args):
        from ..jobs import AdmissionError
        from ..models.manager import ModelUnavailable
        from ..profile.analyzer import analyze
        from ..runners.media import run_media
        from ..util import day_key

        p, user, kind = ctx.platform, ctx.user, self.kind
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult(False, "prompt が空です")
        cost = float(getattr(p.settings.generation, f"{kind}_cost"))
        used = float(p.db.scalar("SELECT generation_units FROM usage_daily WHERE user_id=? AND day=?",
                                 (user["id"], day_key())) or 0)
        if used + cost > user["generation_quota_daily"]:
            return ToolResult(False, f"本日の生成クォータ ({user['generation_quota_daily']}) を超えるため生成できません。"
                                     "ユーザーに明日以降の利用か管理者への相談を伝えてください。")
        a = analyze(prompt, mode=(ctx.job.request or {}).get("mode", "auto"))
        a.task_type = f"{kind}_gen"
        prof = p.profiles.decide_media(a)
        if not prof.model_id:
            return ToolResult(False, f"{self.label}生成モデルがインストールされていないため生成できません")
        over: dict = {"raw_prompt": prompt.isascii()}  # English prompts from the LLM are used as-is
        if args.get("negative_prompt"):
            over["negative"] = str(args["negative_prompt"])[:500]
        if args.get("seed"):
            over["seed"] = int(args["seed"])
        base = prof.media
        if kind in ("image", "video") and args.get("aspect") in ("landscape", "portrait"):
            long_side = max(int(base.get("width", 768)), int(base.get("height", 768)))
            short = long_side * 9 // 16
            over["width"], over["height"] = (long_side, short) if args["aspect"] == "landscape" else (short, long_side)
        elif kind in ("image", "video") and args.get("aspect") == "square":
            side = min(int(base.get("width", 768)), int(base.get("height", 768)))
            over["width"] = over["height"] = side
        if kind == "video" and args.get("seconds"):
            over["frames"] = int(float(args["seconds"]) * int(base.get("fps", 16))) + 1
        if kind == "music" and args.get("seconds"):
            over["seconds"] = float(args["seconds"])
        try:
            rows = await run_media(p, ctx.job, user, prof, prompt, overrides=over)
        except (ModelUnavailable, AdmissionError) as e:
            return ToolResult(False, getattr(e, "message", None) or str(e))
        ctx.assets.extend(rows)
        names = ", ".join(r["name"] for r in rows)
        return ToolResult(bool(rows), f"{self.label}を生成し、ユーザーの画面に表示しました ({names}, モデル: {prof.model_name})。"
                                      "回答では生成した内容を短く説明してください (ファイルのリンクやマークダウン画像は不要)。",
                          {"files": [r["id"] for r in rows]})


ALL_TOOLS: dict[str, Tool] = {t.name: t for t in (WebSearch(), WebFetch(), RunCode(), ReadFile(), WriteFile(),
                                                  ReadWorkspace(), ListWorkspace(), MemorySearch(), MemorySave(),
                                                  GenerateMedia("image"), GenerateMedia("video"), GenerateMedia("music"))}


def schemas(names: list[str]) -> list[dict]:
    return [ALL_TOOLS[n].schema() for n in names if n in ALL_TOOLS]


async def execute(ctx: ToolContext, name: str, raw_args: str | dict, on_event: Callable[[str, dict], None]) -> ToolResult:
    tool = ALL_TOOLS.get(name)
    if tool is None:
        return ToolResult(False, f"不明なツールです: {name}")
    try:
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return ToolResult(False, "ツール引数のJSONが不正です")
    try:
        return await asyncio.wait_for(tool.run(ctx, args), tool.timeout)
    except asyncio.TimeoutError:
        return ToolResult(False, f"ツール {name} がタイムアウトしました")
    except (ToolError, SSRFError, PermissionError) as e:
        return ToolResult(False, str(e))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - tool failures are reported to the model, not raised
        return ToolResult(False, f"ツール実行エラー: {type(e).__name__}: {str(e)[:300]}")
