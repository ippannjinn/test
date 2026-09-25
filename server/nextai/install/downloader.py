"""Resumable, segmented, verified downloads (runs on the target PC, never in the cloud build)."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from ..models.catalog import ComponentSource

SEGMENT_MIN = 64 * 2**20
CHUNK = 1 * 2**20


class DownloadError(Exception):
    pass


@dataclass
class RemoteFile:
    url: str
    path: str
    size: int | None = None
    sha256: str | None = None
    git_sha1: str | None = None
    repo: str = ""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


ProgressFn = Callable[[str, int, int], None]


def make_client(token: str | None = None, host_for_token: str = "huggingface.co") -> httpx.Client:
    headers = {"User-Agent": "NextAI-Platform-Installer/1.0"}
    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=120), headers=headers)
    if token:
        def add_auth(request: httpx.Request) -> None:
            if request.url.host.endswith(host_for_token):
                request.headers["Authorization"] = f"Bearer {token}"
        client.event_hooks["request"] = [add_auth]
    return client


class HFResolver:
    def __init__(self, client: httpx.Client, endpoint: str = "https://huggingface.co"):
        self.client, self.endpoint = client, endpoint.rstrip("/")
        self._cache: dict[str, list[dict]] = {}

    def list_repo(self, repo: str, revision: str = "main") -> list[dict]:
        key = f"{repo}@{revision}"
        if key in self._cache:
            return self._cache[key]
        url = f"{self.endpoint}/api/models/{repo}/tree/{quote(revision, safe='')}?recursive=true&expand=false"
        items: list[dict] = []
        while url:
            r = self.client.get(url)
            if r.status_code in (401, 403, 404):
                raise DownloadError(f"{repo}: アクセスできません (HTTP {r.status_code})")
            r.raise_for_status()
            items += [i for i in r.json() if i.get("type") == "file"]
            nxt = r.links.get("next", {}).get("url")
            url = nxt if nxt and nxt != url else None
        self._cache[key] = items
        return items

    def _remote(self, repo: str, revision: str, item: dict) -> RemoteFile:
        lfs = item.get("lfs") or {}
        return RemoteFile(url=f"{self.endpoint}/{repo}/resolve/{quote(revision, safe='')}/{quote(item['path'])}",
                          path=item["path"], size=int(lfs.get("size") or item.get("size") or 0) or None,
                          sha256=lfs.get("oid") or lfs.get("sha256"),
                          git_sha1=None if lfs else item.get("oid"), repo=repo)

    def resolve(self, sources: list[ComponentSource]) -> list[RemoteFile]:
        errors = []
        for src in sources:
            try:
                files = self.list_repo(src.repo, src.revision)
            except (DownloadError, httpx.HTTPError) as e:
                errors.append(str(e))
                continue
            cands = [f for f in files if not any(fnmatch.fnmatch(f["path"].rsplit("/", 1)[-1], ex) for ex in src.exclude)]

            def match(pat: str) -> list[dict]:
                return sorted((f for f in cands if fnmatch.fnmatch(f["path"], pat)
                               or fnmatch.fnmatch(f["path"].rsplit("/", 1)[-1], pat)), key=lambda f: f["path"])

            if src.mode == "all":
                found = [match(p) for p in src.patterns]
                if all(found):
                    return [self._remote(src.repo, src.revision, m[0]) for m in found]
                errors.append(f"{src.repo}: 必要なファイルが揃っていません")
                continue
            for pat in src.patterns:
                m = match(pat)
                if m:
                    return [self._remote(src.repo, src.revision, m[0])]
            errors.append(f"{src.repo}: パターンに一致するファイルがありません")
        raise DownloadError("ダウンロード元を解決できません: " + " / ".join(errors))


def git_blob_sha1(path: Path) -> str:
    h = hashlib.sha1()
    h.update(f"blob {path.stat().st_size}\0".encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 2**20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 2**20), b""):
            h.update(chunk)
    return h.hexdigest()


class Downloader:
    def __init__(self, client: httpx.Client, progress: ProgressFn | None = None, segments: int = 4,
                 retries: int = 6, cancel: threading.Event | None = None):
        self.client, self.progress, self.segments, self.retries = client, progress, segments, retries
        self.cancel = cancel or threading.Event()

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise DownloadError("ダウンロードが中断されました")

    def _probe(self, url: str) -> tuple[str, int | None, bool]:
        with self.client.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
            if r.status_code == 206:
                total = r.headers.get("content-range", "").rsplit("/", 1)[-1]
                return str(r.url), int(total) if total.isdigit() else None, True
            if r.status_code == 200:
                cl = r.headers.get("content-length")
                return str(r.url), int(cl) if cl and cl.isdigit() else None, r.headers.get("accept-ranges") == "bytes"
            raise DownloadError(f"HTTP {r.status_code}: {url}")

    @staticmethod
    def verified(dest: Path, rf: RemoteFile) -> bool:
        side = dest.with_name(dest.name + ".verified")
        if not dest.exists() or not side.exists():
            return False
        expect = rf.sha256 or rf.git_sha1 or f"size:{rf.size}"
        return side.read_text().strip() == expect and (rf.size is None or dest.stat().st_size == rf.size)

    def download(self, rf: RemoteFile, dest: Path) -> Path:
        if self.verified(dest, rf):
            if self.progress and rf.size:
                self.progress(rf.name, rf.size, rf.size)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        state_f = dest.with_name(dest.name + ".part.json")
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._check_cancel()
            try:
                final_url, size, ranges = self._probe(rf.url)
                size = size or rf.size
                if rf.size and size and rf.size != size:
                    raise DownloadError(f"サイズ不一致 ({size} != {rf.size})")
                if size and ranges and size >= SEGMENT_MIN and self.segments > 1:
                    self._segmented(final_url, part, state_f, size, rf.name)
                else:
                    self._single(final_url, part, size, rf.name, ranges)
                break
            except (httpx.HTTPError, OSError, DownloadError) as e:
                if isinstance(e, DownloadError) and ("中断" in str(e) or "サイズ不一致" in str(e)):
                    raise
                last_err = e
                time.sleep(min(30, 2 ** attempt))
        else:
            raise DownloadError(f"{rf.name} のダウンロードに失敗しました: {last_err}")
        self._verify(part, rf)
        os.replace(part, dest)
        state_f.unlink(missing_ok=True)
        dest.with_name(dest.name + ".verified").write_text(rf.sha256 or rf.git_sha1 or f"size:{dest.stat().st_size}")
        return dest

    def _verify(self, part: Path, rf: RemoteFile) -> None:
        if rf.size is not None and part.stat().st_size != rf.size:
            part.unlink(missing_ok=True)
            raise DownloadError(f"{rf.name}: サイズが一致しません")
        if rf.sha256:
            got = sha256_file(part)
            if got.lower() != rf.sha256.lower():
                part.unlink(missing_ok=True)
                raise DownloadError(f"{rf.name}: SHA256 が一致しません (破損の可能性)")
        elif rf.git_sha1:
            if git_blob_sha1(part) != rf.git_sha1:
                part.unlink(missing_ok=True)
                raise DownloadError(f"{rf.name}: チェックサムが一致しません")

    def _single(self, url: str, part: Path, size: int | None, name: str, ranges: bool) -> None:
        have = part.stat().st_size if part.exists() and ranges else 0
        if size is not None and have >= size:
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self.client.stream("GET", url, headers=headers) as r:
            if have and r.status_code != 206:
                have = 0
            elif r.status_code not in (200, 206):
                raise DownloadError(f"HTTP {r.status_code}")
            with open(part, "ab" if have else "wb") as f:
                done = have
                for chunk in r.iter_bytes(CHUNK):
                    self._check_cancel()
                    f.write(chunk)
                    done += len(chunk)
                    if self.progress:
                        self.progress(name, done, size or 0)

    def _segmented(self, url: str, part: Path, state_f: Path, size: int, name: str) -> None:
        state = None
        if state_f.exists() and part.exists():
            try:
                state = json.loads(state_f.read_text())
                if state.get("size") != size or part.stat().st_size != size:
                    state = None
            except ValueError:
                state = None
        if state is None:
            n = max(1, min(self.segments, size // SEGMENT_MIN))
            step = size // n
            segs = [[i * step, (size if i == n - 1 else (i + 1) * step) - 1, 0] for i in range(n)]
            with open(part, "wb") as f:
                f.truncate(size)
            state = {"size": size, "segments": segs}
            state_f.write_text(json.dumps(state))
        lock = threading.Lock()
        errors: list[Exception] = []
        last_flush = [time.time()]

        def total_done() -> int:
            return sum(s[2] for s in state["segments"])

        def worker(seg: list[int]) -> None:
            start, end = seg[0], seg[1]
            while seg[2] < end - start + 1:
                if self.cancel.is_set() or errors:
                    return
                pos = start + seg[2]
                try:
                    with self.client.stream("GET", url, headers={"Range": f"bytes={pos}-{end}"}) as r:
                        if r.status_code != 206:
                            raise DownloadError(f"Range未対応の応答 HTTP {r.status_code}")
                        with open(part, "r+b") as f:
                            f.seek(pos)
                            for chunk in r.iter_bytes(CHUNK):
                                if self.cancel.is_set():
                                    return
                                f.write(chunk)
                                with lock:
                                    seg[2] += len(chunk)
                                    if self.progress:
                                        self.progress(name, total_done(), size)
                                    if time.time() - last_flush[0] > 2:
                                        state_f.write_text(json.dumps(state))
                                        last_flush[0] = time.time()
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(e)
                    return

        threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in state["segments"]
                   if s[2] < s[1] - s[0] + 1]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        state_f.write_text(json.dumps(state))
        self._check_cancel()
        if errors:
            raise errors[0] if isinstance(errors[0], (httpx.HTTPError, OSError, DownloadError)) else DownloadError(str(errors[0]))
        if total_done() != size:
            raise DownloadError("ダウンロードが未完了です")


def ensure_space(target: Path, need_bytes: int, margin_gb: float) -> None:
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    if free - need_bytes < margin_gb * 2**30:
        raise DownloadError(f"空き容量が不足しています: 必要 {need_bytes / 2**30:.1f}GB + 安全マージン {margin_gb:.0f}GB,"
                            f" 空き {free / 2**30:.1f}GB")
