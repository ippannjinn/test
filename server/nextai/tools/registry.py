"""Tool definitions exposed to the LLM (OpenAI function-calling schema)."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit
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
    presented: dict[str, float] = field(default_factory=dict)  # workspace rel path -> mtime already shown
    research: list[dict] = field(default_factory=list)  # sources found by web_research


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


class WebResearch(Tool):
    name = "web_research"
    description = ("Research a question on the web in one step: runs several searches across providers, reads the best "
                   "pages and returns the most relevant passages as numbered sources [1], [2], ... Prefer this over "
                   "web_search + web_fetch. Cite the numbers in your answer.")
    parameters = {"type": "object", "properties": {
        "question": {"type": "string", "description": "what you want to find out (full question)"},
        "queries": {"type": "array", "items": {"type": "string"},
                    "description": "optional extra search queries (other wording, English, specific names)"}},
        "required": ["question"]}
    timeout = 90

    async def run(self, ctx, args):
        from ..services.research import Researcher

        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult(False, "question が空です")
        extra = [str(q)[:200] for q in (args.get("queries") or [])[:3] if str(q).strip()]
        res = await Researcher(ctx.platform).run(question, queries=extra, emit=ctx.job.emit, budget=lambda: _web_budget(ctx))
        ctx.research.extend(res.source_list())
        if not res.ok:
            return ToolResult(False, f"検索しましたが関連する情報が見つかりませんでした (検索語: {', '.join(res.queries)})。"
                                     "別の言い方・英語・正式名称で queries を指定して再検索してください。")
        return ToolResult(True, res.evidence(), {"sources": res.source_list()})


class WebFetch(Tool):
    name = "web_fetch"
    description = "Fetch a public web page (http/https) and return its readable text."
    parameters = {"type": "object", "properties": {
        "url": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 500, "maximum": 30000},
        "save_as": {"type": "string", "description": "optional: also save the page text into the sandbox workspace "
                                                     "(e.g. research/page1.md) so run_code can analyse it"}},
        "required": ["url"]}
    timeout = 45

    async def run(self, ctx, args):
        _web_budget(ctx)
        try:
            page = await ctx.platform.web.fetch_text(str(args.get("url", "")), int(args.get("max_chars", 10000) or 10000))
        except SSRFError as e:
            return ToolResult(False, f"アクセスが拒否されました: {e}")
        links = "\n".join(f"- {t}: {u}" for t, u in page.get("links", [])[:10])
        saved = ""
        if args.get("save_as") and ctx.workspace is not None:
            p = _ws_path(ctx, str(args["save_as"]))
            body = f"# {page['title']}\nSource: {page['url']}\n\n{page['text']}"
            ctx.platform.files.check_quota(ctx.user, len(body.encode("utf-8")))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            saved = f"\n(saved to /workspace/{p.relative_to(ctx.workspace).as_posix()})"
        return ToolResult(page["status"] < 400,
                          f"URL: {page['url']}\nHTTP {page['status']}\nTitle: {page['title']}{saved}\n\n{page['text']}"
                          + (f"\n\nLinks:\n{links}" if links else ""), {"url": page["url"]})


class DownloadFile(Tool):
    name = "download_file"
    description = ("Download a public file (CSV, JSON, Excel, PDF, image, audio, video, dataset, ...) from http/https into "
                   "the sandbox workspace so run_code / convert_media can process it. Google Drive, Google Docs/Sheets/"
                   "Slides, Dropbox and OneDrive share links work directly (the file must be shared as 'anyone with the "
                   "link'). Internal/LAN addresses are blocked.")
    parameters = {"type": "object", "properties": {
        "url": {"type": "string"}, "save_as": {"type": "string", "description": "relative path, e.g. downloads/data.csv"},
        "format": {"type": "string", "description": "Google Docs/Sheets/Slides only: export format, e.g. pdf, docx, xlsx, csv, pptx"}},
        "required": ["url"]}
    timeout = 120

    async def run(self, ctx, args):
        _web_budget(ctx)
        if ctx.workspace is None:
            return ToolResult(False, "ワークスペースがありません")
        from ..services.cloudlinks import PRIVATE_HINT, direct_url

        url = str(args.get("url", ""))
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in STREAMING_HOSTS):
            return ToolResult(False, STREAMING_NOTE)
        save_as = str(args.get("save_as") or "")
        # Google Drive / Docs / Dropbox / OneDrive share links → the file itself
        fetch_url, service = direct_url(url, str(args.get("format") or "") or Path(save_as).suffix.lstrip("."))
        limit = int(ctx.platform.settings.web.max_download_mb) * 2**20
        try:
            res = await ctx.platform.web.fetch(fetch_url, max_bytes=limit + 1)
        except SSRFError as e:
            return ToolResult(False, f"アクセスが拒否されました: {e}")
        if res.status >= 400:
            return ToolResult(False, f"HTTP {res.status}" + (f" ({service}) " + PRIVATE_HINT if service else ""))
        if len(res.body) > limit:
            return ToolResult(False, f"ファイルが大きすぎます (上限 {limit // 2**20}MB)")
        if "text/html" in (res.content_type or "").lower() and not save_as.endswith((".html", ".htm")):
            if service:
                return ToolResult(False, f"{service}: " + PRIVATE_HINT)
            return ToolResult(False, "この URL はファイルではなく Web ページでした。ページの内容が必要なら web_fetch を使ってください。"
                                     "動画・音声ファイルを変換したい場合は、ファイルそのものの URL か、利用者にファイルをアップロードしてもらってください。")
        fname = re.sub(r"[^\w.\-]", "_", res.filename)[:80].strip("._") if res.filename else ""
        name = save_as or "downloads/" + (fname or _url_name(res.url) or "download.bin")
        p = _ws_path(ctx, name)
        ctx.platform.files.check_quota(ctx.user, len(res.body))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(res.body)
        rel = p.relative_to(ctx.workspace).as_posix()
        return ToolResult(True, f"saved /workspace/{rel} ({len(res.body)} bytes, {res.content_type or 'unknown type'})",
                          {"path": rel})


# Streaming services: downloading their media is against their terms (and usually copies copyrighted works),
# so NextAI doesn't rip from them - the model explains this instead of failing silently.
STREAMING_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "nicovideo.jp", "nico.ms", "tiktok.com",
                   "spotify.com", "soundcloud.com", "netflix.com", "abema.tv", "tver.jp", "twitch.tv", "bilibili.com",
                   "instagram.com", "x.com", "twitter.com", "music.apple.com", "amazon.co.jp", "primevideo.com")
STREAMING_NOTE = ("このサイトは動画・音楽の配信サービスです。配信サービスからのダウンロードや音声の抜き出しは各サービスの利用規約で"
                  "禁止されており、著作権の問題もあるため NextAI では行いません。利用者に次を伝えてください: "
                  "(1) 自分で権利を持つ動画なら、そのファイルをアップロードしてもらえれば mp3 などに変換できる、"
                  "(2) サービス公式のダウンロード / オフライン機能を使う、(3) 内容の要約や説明なら Web で調べて答えられる。")


def _url_name(url: str) -> str:
    from urllib.parse import unquote, urlsplit

    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[^\w.\-]", "_", name)[:80].strip("._")
    return name


# Files a run produces that are worth showing to the user automatically (charts, tables, documents, media).
DELIVERABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".wav", ".mp3", ".ogg", ".mp4", ".webm", ".pdf",
               ".csv", ".xlsx", ".docx", ".pptx", ".zip", ".html", ".md", ".txt", ".json"}
INPUT_DIRS = ("uploads/", "downloads/", "research/")


def present(ctx: ToolContext, rel: str) -> dict | None:
    """Registers a workspace file as a result the user sees (once per version)."""
    p = _ws_path(ctx, rel)
    if not p.is_file():
        raise ToolError(f"ファイルがありません: {rel}")
    mtime = p.stat().st_mtime
    if ctx.presented.get(rel) == mtime:
        return None
    if p.stat().st_size > 50 * 2**20:
        raise ToolError("50MB を超えるファイルは表示できません")
    row = ctx.platform.files.save_path(ctx.user, p, p.name, kind="generated", job_id=ctx.job.id,
                                       meta={"source": "sandbox", "workspace_path": rel})
    ctx.presented[rel] = mtime
    ctx.assets.append(row)
    ctx.job.emit("asset", file_id=row["id"], name=row["name"], mime=row["mime"], kind="file")
    return row


class RunCode(Tool):
    name = "run_code"
    # set by the platform from the active sandbox backend (see SandboxManager.describe)
    python_env = "Python 3 (standard library only)"

    @property
    def description(self) -> str:  # type: ignore[override]
        return (f"Run {self.python_env} in the isolated sandbox: no network, no pip install, limited CPU/RAM/time. "
                "The working directory /workspace persists for this conversation and contains the user's uploads "
                "(uploads/), downloaded data (downloads/) and saved research (research/). Print results to stdout. "
                "New charts/tables/documents you write (png, svg, csv, xlsx, pdf, html, md, ...) are shown to the "
                "user; save matplotlib charts with plt.savefig('chart.png').")
    parameters = {"type": "object", "properties": {"code": {"type": "string", "description": "complete Python program"}},
                  "required": ["code"]}
    timeout = 90

    async def run(self, ctx, args):
        code = str(args.get("code", ""))
        if not code.strip():
            return ToolResult(False, "コードが空です")
        res = await ctx.platform.sandbox.run(ctx.user["id"], code, workspace=ctx.workspace)
        shown = []
        if ctx.workspace is None:
            for name, data in list(res.artifacts.items())[:5]:
                if Path(name).suffix.lower() not in DELIVERABLE:
                    continue
                try:
                    row = ctx.platform.files.save_bytes(ctx.user, Path(name).name, data, kind="generated",
                                                        job_id=ctx.job.id, meta={"source": "sandbox"})
                    ctx.assets.append(row)
                    shown.append(row["name"])
                except Exception:  # noqa: BLE001 - quota errors are reported below
                    res.error = (res.error + " / 出力ファイルを保存できませんでした").strip(" /")
        else:
            for rel in res.files:
                if (Path(rel).suffix.lower() not in DELIVERABLE or rel.startswith(INPUT_DIRS)
                        or any(part.startswith((".", "__")) for part in Path(rel).parts)):
                    continue
                if len(shown) >= 8:
                    break
                try:
                    if present(ctx, rel):
                        shown.append(rel)
                except Exception:  # noqa: BLE001
                    res.error = (res.error + f" / {rel} を表示できませんでした").strip(" /")
        text = res.to_text()
        if shown:
            text += "\n\nshown to the user: " + ", ".join(shown)
        text += _repair_hint(res, self.python_env)
        if ctx.job is not None:  # notebook-style cell in the UI (code, output, duration)
            ctx.job.emit("code_run", code=code[:6000], stdout=res.stdout[:3000], stderr=res.stderr[:2000], ok=res.ok,
                         exit_code=res.exit_code, backend=res.backend, duration=round(res.duration, 2), files=shown)
        return ToolResult(res.ok, text, {"exit_code": res.exit_code, "files": res.files})


def _repair_hint(res, env: str) -> str:
    """Point the model at the fix (self-repair loop) instead of letting it give up or invent the output."""
    err = (res.stderr or "") + (res.error or "")
    if res.ok:
        return ""
    if res.timed_out:
        return "\n\nhint: time limit reached - use a smaller sample / vectorized code and run again."
    m = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", err)
    if m:
        return (f"\n\nhint: '{m.group(1)}' is not available and pip cannot be used. Available: {env}. "
                "Rewrite with those and run again.")
    if "Traceback" in err or res.exit_code not in (0, None):
        return "\n\nhint: read the traceback, fix the cause and run the corrected program again."
    return ""


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
    description = "Create or overwrite a text file in the sandbox workspace (/workspace, relative path)."
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
    description = "Read a text file from the sandbox workspace (relative path)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def run(self, ctx, args):
        p = _ws_path(ctx, str(args.get("path", "")))
        if not p.is_file():
            return ToolResult(False, "ファイルがありません")
        return ToolResult(True, p.read_text(encoding="utf-8", errors="replace")[:20000])


class ListWorkspace(Tool):
    name = "list_workspace"
    description = "List files in the sandbox workspace (uploads/, downloads/, research/ and your outputs)."
    parameters = {"type": "object", "properties": {}}

    async def run(self, ctx, args):
        if ctx.workspace is None:
            return ToolResult(False, "ワークスペースがありません")
        files = [f"{p.relative_to(ctx.workspace).as_posix()} ({p.stat().st_size}B)"
                 for p in sorted(ctx.workspace.rglob("*")) if p.is_file()]
        return ToolResult(True, "\n".join(files) or "(空)")


class ShareFile(Tool):
    name = "share_file"
    description = ("Show / hand a file from the sandbox workspace to the user (e.g. a script, dataset or document they "
                   "asked for). Charts and documents created by run_code are shown automatically.")
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def run(self, ctx, args):
        rel = str(args.get("path", "")).strip().removeprefix("/workspace/")
        row = present(ctx, rel)
        return ToolResult(True, f"shown to the user: {rel}" if row else f"{rel} is already shown")


MEDIA_OUT = {".mp4", ".webm", ".gif", ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".png", ".jpg", ".jpeg", ".webp"}
AUDIO_OUT = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
IMAGE_OUT = {".png", ".jpg", ".jpeg", ".webp"}
DOC_FORMATS = {".md": "markdown", ".markdown": "markdown", ".docx": "docx", ".html": "html", ".htm": "html", ".odt": "odt",
               ".epub": "epub", ".rst": "rst", ".txt": "plain", ".tex": "latex", ".org": "org", ".ipynb": "ipynb"}


async def _external(ctx: ToolContext, name: str, exe: str | None = None):
    from ..services.extools import ToolUnavailable

    try:
        main = await ctx.platform.extools.ensure(name, ctx.job.emit)
    except ToolUnavailable as e:
        raise ToolError(str(e)) from e
    return main if exe is None else (ctx.platform.extools.path(name, exe) or main)


class ConvertMedia(Tool):
    name = "convert_media"
    description = ("Convert / cut / resize video and audio files in the sandbox workspace with ffmpeg (downloaded "
                   "automatically the first time). Examples: video -> mp4/webm/gif, extract audio to mp3, trim a clip, "
                   "grab a frame as png. The output file is shown to the user.")
    parameters = {"type": "object", "properties": {
        "input": {"type": "string", "description": "workspace path, e.g. uploads/movie.mov"},
        "output": {"type": "string", "description": "workspace path; the extension decides the format (" +
                   ", ".join(sorted(MEDIA_OUT)) + ")"},
        "start": {"type": "number", "description": "start time in seconds"},
        "duration": {"type": "number", "description": "length in seconds"},
        "width": {"type": "integer", "minimum": 16, "maximum": 3840, "description": "resize to this width"},
        "fps": {"type": "integer", "minimum": 1, "maximum": 60}},
        "required": ["input", "output"]}
    timeout = 600

    async def run(self, ctx, args):
        src, dst = _ws_path(ctx, str(args.get("input", ""))), _ws_path(ctx, str(args.get("output", "")))
        if not src.is_file():
            return ToolResult(False, f"入力ファイルがありません: {args.get('input')}")
        ext = dst.suffix.lower()
        if ext not in MEDIA_OUT:
            return ToolResult(False, f"出力形式 {ext} には対応していません ({', '.join(sorted(MEDIA_OUT))})")
        ffmpeg = await _external(ctx, "ffmpeg")
        cmd = ["-hide_banner", "-nostdin", "-y", "-protocol_whitelist", "file,pipe"]
        if args.get("start") is not None:
            cmd += ["-ss", f"{max(0.0, float(args['start'])):.3f}"]
        cmd += ["-i", src.name if src.parent == dst.parent else str(src)]
        if args.get("duration") is not None:
            cmd += ["-t", f"{max(0.05, float(args['duration'])):.3f}"]
        filters = []
        if args.get("width"):
            filters.append(f"scale={int(args['width'])}:-2")
        if args.get("fps"):
            filters.append(f"fps={int(args['fps'])}")
        if ext in AUDIO_OUT:
            cmd += ["-vn"]
        else:
            if filters:
                cmd += ["-vf", ",".join(filters)]
            if ext in IMAGE_OUT:
                cmd += ["-frames:v", "1"]
            elif ext == ".mp4":
                cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd.append(str(dst))
        code, out = await ctx.platform.extools.run(ffmpeg, cmd, cwd=src.parent)
        if code != 0 or not dst.exists():
            return ToolResult(False, f"ffmpeg が失敗しました (code {code}):\n{out[-1500:]}")
        ctx.platform.files.check_quota(ctx.user, 0)
        rel = dst.relative_to(ctx.workspace).as_posix()
        present(ctx, rel)
        return ToolResult(True, f"created /workspace/{rel} ({dst.stat().st_size} bytes) and showed it to the user")


class ProbeMedia(Tool):
    name = "probe_media"
    description = "Show duration, resolution, codecs and streams of a video/audio file in the workspace (ffprobe)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    timeout = 120

    async def run(self, ctx, args):
        src = _ws_path(ctx, str(args.get("path", "")))
        if not src.is_file():
            return ToolResult(False, "ファイルがありません")
        probe = await _external(ctx, "ffmpeg", "ffprobe")
        if probe.stem.lower() == "ffprobe":
            code, out = await ctx.platform.extools.run(probe, ["-v", "error", "-show_entries",
                                                               "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
                                                               "-of", "json", str(src)], cwd=src.parent, timeout=60)
        else:
            code, out = await ctx.platform.extools.run(probe, ["-hide_banner", "-nostdin", "-i", str(src)], cwd=src.parent, timeout=60)
            code = 0
        return ToolResult(code == 0, out[-3000:])


class ConvertDocument(Tool):
    name = "convert_document"
    description = ("Convert documents in the sandbox workspace with pandoc (downloaded automatically the first time): "
                   "Markdown / Word (.docx) / HTML / ODT / EPUB / reStructuredText / LaTeX / plain text / Jupyter. "
                   "The output file is shown to the user.")
    parameters = {"type": "object", "properties": {
        "input": {"type": "string"}, "output": {"type": "string", "description": "workspace path; extension = format"}},
        "required": ["input", "output"]}
    timeout = 300

    async def run(self, ctx, args):
        src, dst = _ws_path(ctx, str(args.get("input", ""))), _ws_path(ctx, str(args.get("output", "")))
        if not src.is_file():
            return ToolResult(False, "入力ファイルがありません")
        fin, fout = DOC_FORMATS.get(src.suffix.lower()), DOC_FORMATS.get(dst.suffix.lower())
        if not fin or not fout:
            return ToolResult(False, f"対応形式: {', '.join(sorted(DOC_FORMATS))} (PDF への変換は未対応)")
        pandoc = await _external(ctx, "pandoc")
        dst.parent.mkdir(parents=True, exist_ok=True)
        # --sandbox: pandoc may not read other files or the network while converting
        code, out = await ctx.platform.extools.run(pandoc, ["--sandbox", "-f", fin, "-t", fout, "-o", str(dst), str(src)],
                                                   cwd=src.parent)
        if code != 0 or not dst.exists():
            return ToolResult(False, f"pandoc が失敗しました (code {code}):\n{out[-1500:]}")
        rel = dst.relative_to(ctx.workspace).as_posix()
        present(ctx, rel)
        return ToolResult(True, f"created /workspace/{rel} and showed it to the user")


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


ALL_TOOLS: dict[str, Tool] = {t.name: t for t in (WebResearch(), WebSearch(), WebFetch(), RunCode(), ReadFile(), WriteFile(),
                                                  ReadWorkspace(), ListWorkspace(), ShareFile(), DownloadFile(),
                                                  ConvertMedia(), ProbeMedia(), ConvertDocument(),
                                                  MemorySearch(), MemorySave(),
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
