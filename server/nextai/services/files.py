"""Per-user file namespaces with quota enforcement and text extraction."""
from __future__ import annotations

import hashlib
import html
import io
import mimetypes
import os
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from ..config import Settings
from ..db import Database
from ..util import dumps, loads, new_id, now

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm", ".log",
            ".ini", ".toml", ".cfg", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
            ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".bat", ".sql", ".css", ".scss", ".kt", ".swift",
            ".r", ".lua", ".pl", ".vue", ".svelte", ".tex", ".srt", ".vtt"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
INLINE_MIMES = IMAGE_MIMES | {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/ogg", "video/webm", "video/mp4"}
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_ZIP_MEMBER = 64 * 2**20


class QuotaExceeded(Exception):
    pass


class FileTooLarge(Exception):
    pass


def sanitize_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name or "file")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip(" .") or "file"
    return name[:200]


def sniff_mime(head: bytes, name: str) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    guessed = mimetypes.guess_type(name)[0]
    if head.startswith(b"PK\x03\x04"):
        return guessed if guessed and ("officedocument" in guessed or guessed.endswith("zip")) else "application/zip"
    ext = Path(name).suffix.lower()
    if ext in TEXT_EXT:
        return "text/plain" if ext not in (".html", ".htm", ".svg") else "text/html"
    if guessed in IMAGE_MIMES:
        return "application/octet-stream"  # extension claims image but magic bytes disagree
    return guessed or "application/octet-stream"


class FileStore:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db

    # ------------------------------------------------------------------ paths
    def user_root(self, user_id: str) -> Path:
        if not _ID_RE.match(user_id):
            raise ValueError("invalid user id")
        root = self.settings.paths.users / user_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    def safe_path(self, user_id: str, rel: str) -> Path:
        root = self.user_root(user_id).resolve()
        p = (root / rel).resolve()
        if os.path.commonpath([str(root), str(p)]) != str(root):
            raise PermissionError("path escapes user namespace")
        return p

    def workspace(self, user_id: str, job_id: str) -> Path:
        if not _ID_RE.match(job_id):
            raise ValueError("invalid job id")
        p = self.safe_path(user_id, f"workspaces/{job_id}")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def conv_workspace(self, user_id: str, conv_id: str) -> Path:
        """Per-conversation sandbox workspace (mounted as /workspace for run_code; persists across turns)."""
        if not _ID_RE.match(conv_id or ""):
            raise ValueError("invalid conversation id")
        p = self.safe_path(user_id, f"workspaces/conv-{conv_id}")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def has_conv_workspace(self, user_id: str, conv_id: str) -> bool:
        if not _ID_RE.match(conv_id or ""):
            return False
        p = self.safe_path(user_id, f"workspaces/conv-{conv_id}")
        return p.is_dir() and any(f.is_file() for f in p.rglob("*"))

    def delete_conv_workspace(self, user_id: str, conv_id: str) -> None:
        if _ID_RE.match(conv_id or ""):
            shutil.rmtree(self.safe_path(user_id, f"workspaces/conv-{conv_id}"), ignore_errors=True)

    # ------------------------------------------------------------------ quota
    def usage_bytes(self, user_id: str) -> int:
        files = int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM files WHERE user_id=?", (user_id,)) or 0)
        ws = self.user_root(user_id) / "workspaces"
        extra = sum(f.stat().st_size for f in ws.rglob("*") if f.is_file()) if ws.exists() else 0
        return files + extra

    def check_quota(self, user: dict, add_bytes: int) -> None:
        quota = int(user["storage_quota_mb"]) * 2**20
        if self.usage_bytes(user["id"]) + add_bytes > quota:
            raise QuotaExceeded(f"ストレージ容量の上限 ({user['storage_quota_mb']}MB) を超えます")

    # ------------------------------------------------------------------ write
    def _insert(self, user_id: str, kind: str, name: str, mime: str, size: int, sha: str, rel: str,
                job_id: str | None, meta: dict | None) -> dict:
        fid = rel.rsplit("/", 1)[-1].split(".")[0]
        self.db.execute(
            "INSERT INTO files(id, user_id, kind, name, mime, size, sha256, rel_path, job_id, meta, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, user_id, kind, name, mime, size, sha, rel, job_id, dumps(meta or {}), now()))
        return self.db.one("SELECT * FROM files WHERE id=?", (fid,))

    def save_stream(self, user: dict, stream: BinaryIO, name: str, *, kind: str = "upload",
                    max_bytes: int | None = None, job_id: str | None = None, meta: dict | None = None) -> dict:
        name = sanitize_name(name)
        max_bytes = max_bytes or self.settings.server.max_upload_mb * 2**20
        root = self.user_root(user["id"])
        fid = new_id()
        ext = Path(name).suffix.lower()[:12]
        safe_ext = ext if re.match(r"^\.[a-z0-9]+$", ext) else ""
        rel = f"files/{fid}{safe_ext}"
        dest = self.safe_path(user["id"], rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = root / f".upload-{fid}"
        h, size, head = hashlib.sha256(), 0, b""
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if len(head) < 64:
                        head += chunk[:64 - len(head)]
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLarge(f"ファイルサイズの上限 ({max_bytes // 2**20}MB) を超えています")
                    h.update(chunk)
                    out.write(chunk)
            self.check_quota(user, size)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return self._insert(user["id"], kind, name, sniff_mime(head, name), size, h.hexdigest(), rel, job_id, meta)

    def save_bytes(self, user: dict, name: str, data: bytes, *, kind: str = "generated", job_id: str | None = None,
                   meta: dict | None = None) -> dict:
        return self.save_stream(user, io.BytesIO(data), name, kind=kind, max_bytes=max(len(data), 1), job_id=job_id, meta=meta)

    def save_path(self, user: dict, src: Path, name: str, *, kind: str = "generated", job_id: str | None = None,
                  meta: dict | None = None) -> dict:
        size = src.stat().st_size
        with open(src, "rb") as f:
            return self.save_stream(user, f, name, kind=kind, max_bytes=max(size, 1), job_id=job_id, meta=meta)

    # ------------------------------------------------------------------ read
    def get(self, user_id: str, file_id: str) -> dict | None:
        if not _ID_RE.match(file_id or ""):
            return None
        row = self.db.one("SELECT * FROM files WHERE id=? AND user_id=?", (file_id, user_id))
        if row:
            row["meta"] = loads(row["meta"], {})
        return row

    def list(self, user_id: str, kind: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM files WHERE user_id=?"
        params: list[Any] = [user_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [min(max(limit, 1), 1000), max(offset, 0)]
        rows = self.db.query(sql, tuple(params))
        for r in rows:
            r["meta"] = loads(r["meta"], {})
        return rows

    def path_of(self, row: dict) -> Path:
        return self.safe_path(row["user_id"], row["rel_path"])

    def delete(self, user_id: str, file_id: str) -> bool:
        row = self.get(user_id, file_id)
        if not row:
            return False
        p = self.path_of(row)
        for extra in (p, p.with_name(p.name + ".txt")):
            if extra.exists():
                extra.unlink()
        self.db.execute("DELETE FROM files WHERE id=? AND user_id=?", (file_id, user_id))
        return True

    def delete_all(self, user_id: str) -> None:
        root = self.settings.paths.users / user_id
        if _ID_RE.match(user_id) and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        self.db.execute("DELETE FROM files WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------------ text extraction
    def extract_text(self, row: dict, max_chars: int = 200_000) -> str:
        p = self.path_of(row)
        cache = p.with_name(p.name + ".txt")
        if cache.exists():
            return cache.read_text(encoding="utf-8")[:max_chars]
        text = extract_text_from_path(p, row["name"], row["mime"])
        try:
            cache.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text[:max_chars]


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8", "cp932", "euc_jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


def _xml_text(xml: str, para_tag: str) -> str:
    xml = re.sub(rf"</{para_tag}>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _zip_member(z: zipfile.ZipFile, name: str) -> str:
    info = z.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER:
        return ""
    return z.read(name).decode("utf-8", "replace")


def extract_text_from_path(p: Path, name: str, mime: str) -> str:
    ext = Path(name).suffix.lower()
    try:
        if ext in TEXT_EXT or mime.startswith("text/"):
            with open(p, "rb") as f:
                return _decode(f.read(8 * 2**20))
        if mime == "application/pdf" or ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(p))
            parts = []
            for i, page in enumerate(reader.pages[:300]):
                parts.append(f"--- page {i + 1} ---\n{page.extract_text() or ''}")
            return "\n".join(parts)
        if ext in (".docx", ".xlsx", ".pptx"):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                if ext == ".docx" and "word/document.xml" in names:
                    return _xml_text(_zip_member(z, "word/document.xml"), "w:p")
                if ext == ".xlsx":
                    out = []
                    if "xl/sharedStrings.xml" in names:
                        out.append(_xml_text(_zip_member(z, "xl/sharedStrings.xml"), "si"))
                    for n in sorted(x for x in names if x.startswith("xl/worksheets/sheet"))[:20]:
                        vals = re.findall(r"<v>([^<]*)</v>", _zip_member(z, n))
                        out.append(f"--- {n} ---\n" + " ".join(vals[:5000]))
                    return "\n".join(out)
                if ext == ".pptx":
                    slides = sorted((x for x in names if re.match(r"ppt/slides/slide\d+\.xml$", x)),
                                    key=lambda s: int(re.findall(r"\d+", s)[-1]))
                    return "\n".join(f"--- slide {i + 1} ---\n" + _xml_text(_zip_member(z, s), "a:p")
                                     for i, s in enumerate(slides[:300]))
    except Exception as e:  # noqa: BLE001
        return f"(テキスト抽出に失敗しました: {type(e).__name__})"
    return ""
