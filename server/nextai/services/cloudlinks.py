"""Share links of cloud storage → direct download URLs (Google Drive / Docs / Sheets / Slides, Dropbox, OneDrive).

Only links the owner shared as "anyone with the link" work: NextAI never signs in to anyone's account.
"""
from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_DRIVE_ID = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{10,})")
_DOCS = {"document": ("docx", "document"), "spreadsheets": ("xlsx", "spreadsheet"), "presentation": ("pptx", "presentation")}

PRIVATE_HINT = ("共有リンクの先がファイルではなくログイン画面や確認ページでした。ファイルの共有設定を"
                "「リンクを知っている全員が閲覧可」にしてもらうか、ファイルを直接アップロードしてもらってください。")


def direct_url(url: str, want: str = "") -> tuple[str, str]:
    """(direct download URL, service name) — or (url, "") when it is not a known share link.
    `want` picks the export format of Google Docs/Sheets/Slides (e.g. "pdf", "csv"); default is the Office format."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    qs = parse_qs(parts.query)
    if host in ("drive.google.com", "docs.google.com") or host.endswith(".drive.google.com"):
        m = re.match(r"/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{10,})", parts.path)
        if host == "docs.google.com" and m:
            kind, fid = m.group(1), m.group(2)
            fmt = (want or _DOCS[kind][0]).lower()
            q = {"format": fmt}
            if kind == "spreadsheets" and fmt in ("csv", "tsv") and "gid" in qs:
                q["gid"] = qs["gid"][0]
            return f"https://docs.google.com/{kind}/d/{fid}/export?{urlencode(q)}", "Google " + _DOCS[kind][1]
        fid = (qs.get("id") or [""])[0]
        if not fid:
            m = _DRIVE_ID.search(parts.path)
            fid = m.group(1) if m else ""
        if fid and re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid):
            # the usercontent endpoint skips the "can't scan this large file for viruses" page
            return (f"https://drive.usercontent.google.com/download?{urlencode({'id': fid, 'export': 'download', 'confirm': 't'})}",
                    "Google Drive")
        return url, ""
    if host in ("www.dropbox.com", "dropbox.com"):
        q = {k: v[-1] for k, v in qs.items()}
        q["dl"] = "1"
        return urlunsplit(("https", "www.dropbox.com", parts.path, urlencode(q), "")), "Dropbox"
    if host in ("1drv.ms", "onedrive.live.com"):
        token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content", "OneDrive"
    return url, ""
