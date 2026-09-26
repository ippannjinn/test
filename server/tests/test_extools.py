import io
import json
import stat
import sys
import tarfile

from conftest import create_member, wait_job, web_login

from nextai.install.runtime import RuntimeInstaller

# A stand-in for ffmpeg / pandoc: records its argv and writes the output file (last argument / -o value).
FAKE = """#!{py}
import sys, json, pathlib
args = sys.argv[1:]
pathlib.Path(sys.argv[0]).with_suffix('.log').write_text(json.dumps(args))
out = args[args.index('-o') + 1] if '-o' in args else args[-1]
pathlib.Path(out).write_bytes(b'converted')
"""


def _tar_with(tool: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = FAKE.format(py=sys.executable).encode()
        info = tarfile.TarInfo(f"{tool}-build/bin/{tool}")
        info.size, info.mode = len(data), 0o755
        t.addfile(info, io.BytesIO(data))
        evil = tarfile.TarInfo("../escape.txt")
        evil.size = 1
        t.addfile(evil, io.BytesIO(b"x"))
    return buf.getvalue()


def _fake_releases(monkeypatch, tmp_path):
    def releases(self, repo):
        name = "ffmpeg-master-latest-linux64-gpl.tar.xz" if "FFmpeg" in repo else "pandoc-3.8-linux-amd64.tar.gz"
        return [{"tag_name": "v-test", "assets": [{"name": name, "browser_download_url": "https://example.invalid/x"}]}]

    def download(self, asset, dest):
        tool = "ffmpeg" if "ffmpeg" in asset["name"] else "pandoc"
        p = tmp_path / asset["name"]
        p.write_bytes(_tar_with(tool))
        return p

    monkeypatch.setattr(RuntimeInstaller, "_releases", releases)
    monkeypatch.setattr(RuntimeInstaller, "_download_asset", download)


def _send(client, conv, text):
    r = client.post(f"/api/conversations/{conv}/messages", json={"content": text}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    return r["conversation_id"], job, client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]


def _tool(name, **a):
    return f"[[tool:{name} {json.dumps(a, ensure_ascii=False)}]]"


def test_ffmpeg_installed_on_first_use_and_used_safely(client, platform, monkeypatch, tmp_path):
    _fake_releases(monkeypatch, tmp_path)
    create_member(platform, "editor")
    web_login(client, "editor")
    assert platform.extools.path("ffmpeg") is None
    up = client.post("/api/files", files={"file": ("clip.mov", b"MOVDATA", "video/quicktime")}).json()["file"]
    r = client.post("/api/conversations/new/messages", json={
        "content": "この動画の音声をmp3にして実行して " + _tool("convert_media", input="uploads/clip.mov", output="audio.mp3", start=1.5),
        "attachments": [up["id"]]}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    msg = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]
    assert job["status"] == "done", job
    exe = platform.extools.path("ffmpeg")
    assert exe and exe.stat().st_mode & stat.S_IXUSR
    assert not (platform.settings.paths.runtime / "tools" / "escape.txt").exists()
    assert [a["name"] for a in msg["meta"]["assets"]] == ["audio.mp3"]
    argv = json.loads(exe.with_suffix(".log").read_text())
    assert argv[:5] == ["-hide_banner", "-nostdin", "-y", "-protocol_whitelist", "file,pipe"]
    assert "-vn" in argv and argv[argv.index("-ss") + 1] == "1.500"
    # pandoc: markdown -> docx, with --sandbox
    conv, job, msg = _send(client, r["conversation_id"], "文書を作って " + _tool("write_file", path="doc.md", content="# 見出し"))
    conv, job, msg = _send(client, conv, "変換して " + _tool("convert_document", input="doc.md", output="doc.docx"))
    assert [a["name"] for a in msg["meta"]["assets"]] == ["doc.docx"]
    argv = json.loads(platform.extools.path("pandoc").with_suffix(".log").read_text())
    assert argv[0] == "--sandbox" and argv[1:5] == ["-f", "markdown", "-t", "docx"]


def test_tools_respect_admin_settings(client, platform, monkeypatch, tmp_path):
    _fake_releases(monkeypatch, tmp_path)
    platform.settings.load_overrides({"tools.auto_install": False})
    create_member(platform, "editor2")
    web_login(client, "editor2")
    conv, job, msg = _send(client, "new", "コードを書いて実行して " + _tool("run_code", code="open('a.md','w').write('x')"))
    conv, job, msg = _send(client, conv, "変換して " + _tool("convert_document", input="a.md", output="a.html"))
    assert "自動インストールは無効" in msg["content"] and platform.extools.path("pandoc") is None
    platform.settings.load_overrides({"tools.auto_install": True, "tools.allowed": ["ffmpeg"]})
    conv, job, msg = _send(client, conv, "変換して " + _tool("convert_document", input="a.md", output="a.html"))
    assert "許可されていません" in msg["content"]


def test_conversion_requests_unlock_tools_and_streaming_sites_are_refused(client, platform, monkeypatch):
    from nextai.profile.analyzer import analyze

    a = analyze("この動画をmp3にして")
    prof = platform.profiles.decide(a)
    assert a.needs_convert and "convert_media" in prof.tools and prof.use_agent
    b = analyze("https://example.com/files/talk.mp4 これを音声だけにして")
    assert "download_file" in platform.profiles.decide(b).tools
    assert analyze("このMarkdownをWordに変換して").needs_convert
    assert not analyze("今日の天気は？").needs_convert
    # YouTube: the download tool refuses with an explanation instead of fetching
    create_member(platform, "ytuser")
    web_login(client, "ytuser")
    conv, job, msg = _send(client, "new", "https://youtu.be/abc これをmp3にして " + _tool("download_file", url="https://youtu.be/abc"))
    assert job["status"] == "done" and "利用規約" in msg["content"]


def test_convert_fallback_when_model_skips_tool(client, platform, monkeypatch, tmp_path):
    _fake_releases(monkeypatch, tmp_path)
    create_member(platform, "lazy")
    web_login(client, "lazy")
    up = client.post("/api/files", files={"file": ("rec.mov", b"MOV", "video/quicktime")}).json()["file"]
    r = client.post("/api/conversations/new/messages", json={"content": "この動画をmp3にして", "attachments": [up["id"]]}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    msg = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]
    assert job["status"] == "done" and [a["name"] for a in msg["meta"]["assets"]] == ["rec.mp3"]
