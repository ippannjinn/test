import asyncio
import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

from nextai.backup import create_backup, list_backups, restore_now
from nextai.config import Settings
from nextai.install.downloader import Downloader, DownloadError, HFResolver, RemoteFile, make_client
from nextai.install.runtime import pick_llama_assets, pick_sd_assets
from nextai.models.catalog import ComponentSource
from nextai.resources.governor import Governor, Level
from nextai.resources.monitor import GpuInfo, Snapshot
from nextai.security.ssrf import SSRFError, ip_is_public, validate_url
from nextai.security.tls import ensure_server_cert, fingerprint_sha256
from nextai.tools.sandbox import ProcessBackend, SandboxManager, WasmPythonBackend

from conftest import create_member, make_settings, web_login


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8443/", "http://192.168.1.10/", "http://10.0.0.1/", "http://[::1]/",
    "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "http://user:pw@example.com/",
    "http://printer.local/", "http://intranet/", "gopher://example.com/", "http://[::ffff:127.0.0.1]/",
    "http://100.100.100.100/", "http://example.com:22/",
])
def test_ssrf_blocks_internal(url):
    with pytest.raises(SSRFError):
        validate_url(url, [80, 443, 8080, 8443])


def test_ssrf_allows_public():
    assert validate_url("https://example.com/path?q=1", [443])[1] == "example.com"
    assert ip_is_public("93.184.216.34") and not ip_is_public("10.1.2.3")


def test_security_headers_and_body_limit(client, platform):
    r = client.get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    create_member(platform)
    web_login(client, "alice")
    big = "x" * (platform.settings.server.max_json_kb * 1024 + 10)
    r = client.post("/api/memory", content=json.dumps({"content": big}), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_sql_injection_is_inert(client, platform):
    create_member(platform)
    web_login(client, "alice")
    r = client.get("/api/conversations", params={"q": "' OR 1=1; DROP TABLE users; --"})
    assert r.status_code == 200
    assert platform.db.scalar("SELECT COUNT(*) FROM users") == 1


def test_governor_levels_and_budget(tmp_path):
    s = make_settings(tmp_path, resources={"ram_elevated_mb": 6000, "ram_high_mb": 4000, "ram_critical_mb": 2500,
                                           "disk_margin_gb": 15, "disk_low_buffer_gb": 5})
    g = Governor(s)
    gpu = GpuInfo(0, "RTX", 12227, 4000, temp_c=60)
    snap = lambda ram, disk=100.0, own=3000, temp=60: Snapshot(0, 10, 20, 32000, ram, 500, disk,
                                                               [GpuInfo(0, "RTX", 12227, 4000, temp_c=temp)], own)
    st = g.evaluate(snap(20000))
    assert st.level == Level.NORMAL and st.vram_budget_mb == 12227 - 1024 - 1000
    assert g.evaluate(snap(3000)).level == Level.HIGH
    assert not g.state.allow_heavy
    assert g.evaluate(snap(2000)).level == Level.CRITICAL and not g.state.accept_jobs
    for _ in range(2):
        assert g.evaluate(snap(20000)).level == Level.CRITICAL  # hysteresis
    assert g.evaluate(snap(20000)).level < Level.CRITICAL
    assert g.evaluate(snap(20000, disk=12)).disk == "critical"
    assert g.evaluate(snap(20000, temp=90)).gpu_paused
    del gpu


def test_tls_cert_generation(tmp_path):
    crt, key = ensure_server_cert(tmp_path, ["192.168.1.50"])
    assert crt.exists() and key.exists()
    first = crt.read_bytes()
    assert len(fingerprint_sha256(tmp_path / "ca.crt")) == 95
    crt2, _ = ensure_server_cert(tmp_path, ["192.168.1.50"])
    assert crt2.read_bytes() == first  # reused when SANs still match
    crt3, _ = ensure_server_cert(tmp_path, ["192.168.1.60"])
    assert crt3.read_bytes() != first


def test_process_sandbox_limits(tmp_path):
    b = ProcessBackend()
    work = tmp_path / "w"
    work.mkdir()
    (work / "main.py").write_text("print('hello'); open('out.txt','w').write('x')")
    r = b.run_sync(work, 5, 256, 4096)
    assert r.ok and "hello" in r.stdout
    (work / "main.py").write_text("while True: pass")
    r = b.run_sync(work, 1, 256, 4096)
    assert r.timed_out and not r.ok
    (work / "main.py").write_text("x = bytearray(1024*1024*1024)")
    r = b.run_sync(work, 5, 128, 4096)
    assert not r.ok


async def test_sandbox_manager_artifacts(tmp_path):
    s = make_settings(tmp_path)
    s.paths.ensure()
    m = SandboxManager(s)
    r = await m.run("a" * 32, "open('result.csv','w').write('a,b\\n1,2')\nprint('done')")
    assert r.ok and r.files == ["result.csv"] and r.artifacts["result.csv"].startswith(b"a,b")


def test_wasm_sandbox_limits_with_wat(tmp_path):
    import wasmtime

    loop_wat = """(module (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))
      (memory (export "memory") 1) (func (export "_start") (loop $l (br $l))))"""
    exit_wat = """(module (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))
      (memory (export "memory") 1) (func (export "_start") (call $exit (i32.const 3))))"""
    grow_wat = """(module (memory (export "memory") 1)
      (func (export "_start") (if (i32.eq (memory.grow (i32.const 20000)) (i32.const -1)) (then unreachable))))"""
    for name, wat in (("loop", loop_wat), ("exit", exit_wat), ("grow", grow_wat)):
        (tmp_path / f"{name}.wasm").write_bytes(wasmtime.wat2wasm(wat))
    work = tmp_path / "w"
    work.mkdir()
    r = WasmPythonBackend(tmp_path / "loop.wasm", tmp_path / "cache").run_sync(work, 0.3, 64, 1000)
    assert r.timed_out
    r = WasmPythonBackend(tmp_path / "exit.wasm", tmp_path / "cache").run_sync(work, 2, 64, 1000)
    assert r.exit_code == 3
    r = WasmPythonBackend(tmp_path / "grow.wasm", tmp_path / "cache").run_sync(work, 2, 64, 1000)
    assert not r.ok and not r.timed_out


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    data = b""
    fail_once = {"n": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/models/"):
            items = [{"type": "file", "path": "model-Q4_K_M.gguf", "size": len(self.data),
                      "lfs": {"oid": hashlib.sha256(self.data).hexdigest(), "size": len(self.data)}},
                     {"type": "file", "path": "mmproj-f16.gguf", "size": 10, "lfs": {"oid": "0" * 64, "size": 10}},
                     {"type": "file", "path": "config.json", "size": 2, "oid": hashlib.sha1(b"blob 2\0{}").hexdigest()}]
            body = json.dumps(items).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        rng = self.headers.get("Range")
        data = self.data
        if rng:
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), int(b) if b else len(data) - 1
            chunk = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        else:
            chunk = data
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if _RangeHandler.fail_once["n"] > 0 and len(chunk) > 1000:
            _RangeHandler.fail_once["n"] -= 1
            self.wfile.write(chunk[: len(chunk) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(chunk)


@pytest.fixture
def http_server():
    _RangeHandler.data = os.urandom(3 * 1024 * 1024 + 123)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_downloader_resume_segmented_and_verify(tmp_path, http_server, monkeypatch):
    import nextai.install.downloader as dl

    monkeypatch.setattr(dl, "SEGMENT_MIN", 512 * 1024)
    data = _RangeHandler.data
    client = make_client()
    client._trust_env = False
    rf = RemoteFile(url=http_server + "/f.bin", path="f.bin", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    _RangeHandler.fail_once["n"] = 2
    progress = []
    d = Downloader(client, lambda n, done, total: progress.append(done), segments=4, retries=4)
    out = d.download(rf, tmp_path / "f.bin")
    assert out.read_bytes() == data and progress
    assert Downloader.verified(out, rf)
    # single-stream resume from a partial file
    part = tmp_path / "g.bin.part"
    part.write_bytes(data[:1000])
    monkeypatch.setattr(dl, "SEGMENT_MIN", 10**12)
    out2 = d.download(RemoteFile(url=http_server + "/g", path="g.bin", size=len(data),
                                 sha256=hashlib.sha256(data).hexdigest()), tmp_path / "g.bin")
    assert out2.read_bytes() == data
    with pytest.raises(DownloadError):
        Downloader(client, retries=1).download(RemoteFile(url=http_server + "/h", path="h.bin", size=len(data),
                                                          sha256="0" * 64), tmp_path / "h.bin")
    assert not (tmp_path / "h.bin").exists()


def test_hf_resolver_patterns(http_server):
    client = make_client()
    client._trust_env = False
    r = HFResolver(client, http_server)
    files = r.resolve([ComponentSource(repo="org/missing-first", patterns=["nothing*.gguf"]),
                       ComponentSource(repo="org/repo", patterns=["*Q4_K_M.gguf"], exclude=["mmproj*"])])
    assert files[0].path == "model-Q4_K_M.gguf" and files[0].sha256
    snap = r.resolve([ComponentSource(repo="org/repo", patterns=["config.json", "*Q4_K_M.gguf"], mode="all")])
    assert [f.path for f in snap] == ["config.json", "model-Q4_K_M.gguf"] and snap[0].git_sha1
    with pytest.raises(DownloadError):
        r.resolve([ComponentSource(repo="org/repo", patterns=["*.safetensors"])])


def test_runtime_asset_selection(monkeypatch):
    import nextai.install.runtime as rt

    monkeypatch.setattr(rt, "IS_WIN", True)
    assets = [{"name": n} for n in ("llama-b6500-bin-win-cuda-12.4-x64.zip", "llama-b6500-bin-win-cuda-13.1-x64.zip",
                                    "cudart-llama-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-13.1-x64.zip",
                                    "llama-b6500-bin-win-vulkan-x64.zip", "llama-b6500-bin-win-cpu-x64.zip")]
    accel, main, cudart = rt.pick_llama_assets(assets, "12.9")
    assert accel == "cuda-12.4" and cudart["name"].endswith("12.4-x64.zip")
    accel, main, _ = rt.pick_llama_assets(assets, "13.2")
    assert accel == "cuda-13.1"
    assert rt.pick_llama_assets(assets, None)[0] == "vulkan"
    sd = [{"name": n} for n in ("sd-master-abc-bin-win-cuda12-x64.zip", "sd-master-abc-bin-win-vulkan-x64.zip",
                                "sd-master-abc-bin-win-avx2-x64.zip", "cudart-sd-bin-win-cu12-x64.zip")]
    accel, main, cudart = rt.pick_sd_assets(sd, "12.8")
    assert accel == "cuda12" and cudart
    assert rt.pick_sd_assets(sd, None)[0] == "vulkan"


def test_backup_and_restore(tmp_path, platform):
    create_member(platform)
    uid = platform.auth.get_user_by_name("alice")["id"]
    platform.files.save_bytes(platform.auth.get_user(uid), "a.txt", b"hello", kind="upload")
    path = create_backup(platform.settings, platform.db)
    assert list_backups(platform.settings)[0]["name"] == path.name
    platform.db.execute("DELETE FROM users")
    platform.db.close()
    restore_now(platform.settings, path)
    from nextai.db import Database

    db = Database(platform.settings.paths.db)
    assert db.scalar("SELECT COUNT(*) FROM users") == 1
    assert (platform.settings.paths.users / uid / "files").exists()


def test_update_check_prefers_newest_of_api_and_cached_link(monkeypatch):
    import asyncio

    import httpx

    from nextai.api import admin as adm

    manifests = {"https://github.com/o/r/releases/download/v1.8.0/update-manifest.json": {"version": "1.8.0"},
                 "https://github.com/o/r/releases/latest/download/update-manifest.json": {"version": "1.7.1"}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=[{"draft": False, "prerelease": False, "assets": [
                {"name": "update-manifest.json",
                 "browser_download_url": "https://github.com/o/r/releases/download/v1.8.0/update-manifest.json"}]}])
        key = str(req.url.copy_with(query=None))
        return httpx.Response(200, json=manifests[key])

    orig = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return orig(*a, **k)

    monkeypatch.setattr(adm.httpx, "AsyncClient", client)
    m = asyncio.run(adm._latest_manifest("https://github.com/o/r/releases/latest/download/update-manifest.json"))
    assert m["version"] == "1.8.0"


def test_web_ui_files_revalidate(client):
    r = client.get("/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
