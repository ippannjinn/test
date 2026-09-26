"""Exercises the real llama.cpp / sd.cpp adapters against protocol-compatible fake executables."""
import asyncio
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from PIL import Image

from nextai.backends.base import ChatRequest
from nextai.backends.llamacpp import LlamaCppBackend
from nextai.backends.media import SdCppBackend, extract_mjpeg_frames
from nextai.models.catalog import Catalog
from nextai.models.planner import LaunchPlan

from conftest import make_settings, offline_web

FAKE_LLAMA = r'''
import json, sys, http.server, threading
args = sys.argv[1:]
if "--help" in args:
    print("-m, --model FNAME\n-fa, --flash-attn [on|off|auto]\n--n-cpu-moe N\n--cache-reuse N\n--kv-unified\n--no-webui\n--metrics\n--embeddings\n--jinja")
    sys.exit(0)
port = int(args[args.index("--port") + 1]); key = args[args.index("--api-key") + 1]
open(ARGS_FILE, "w").write(json.dumps(args))
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _auth(self):
        if self.headers.get("Authorization") != "Bearer " + key:
            self.send_response(401); self.end_headers(); return False
        return True
    def do_GET(self):
        if not self._auth(): return
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def do_POST(self):
        if not self._auth(): return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/embeddings":
            data = [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(body["input"])]
            out = json.dumps({"data": data}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        def ev(o): self.wfile.write(("data: " + json.dumps(o) + "\n\n").encode()); self.wfile.flush()
        if body.get("tools") and body["messages"][-1]["role"] != "tool":
            ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_code", "arguments": "{\"code\": "}}]}}]})
            ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"print(1)\"}"}}]}, "finish_reason": "tool_calls"}]})
        else:
            ev({"choices": [{"delta": {"reasoning_content": "thinking"}}]})
            for part in ["こんにちは", "、", "世界"]:
                ev({"choices": [{"delta": {"content": part}}]})
            ev({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "timings": {"predicted_per_second": 55.5}, "echo": body.get("chat_template_kwargs")})
        self.wfile.write(b"data: [DONE]\n\n")
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''

FAKE_SD = r'''
import sys, io
from PIL import Image
args = sys.argv[1:]
if "--help" in args:
    print("--diffusion-fa\n--vae-tiling\n--clip-on-cpu\n--offload-to-cpu"); sys.exit(0)
open(ARGS_FILE, "w").write("\n".join(args))
out = args[args.index("-o") + 1]
for i in range(1, 5):
    print(f"  |{'=' * i}| {i}/4 - 0.10s/it", flush=True)
if "vid_gen" in args:
    with open(out, "wb") as f:
        f.write(b"RIFF....AVI LIST")
        for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
            buf = io.BytesIO(); Image.new("RGB", (32, 32), c).save(buf, "JPEG"); f.write(b"00dc" + buf.getvalue())
else:
    Image.new("RGB", (64, 64), (10, 20, 30)).save(out)
'''


def _fake(tmp_path: Path, component: str, name: str, code: str, args_file: Path) -> Path:
    d = tmp_path / "data" / "runtime" / component / "test"
    d.mkdir(parents=True, exist_ok=True)
    exe = d / name
    exe.write_text(f"#!{sys.executable}\nARGS_FILE = {str(args_file)!r}\n" + code)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


@pytest.mark.skipif(os.name == "nt", reason="uses a shebang script as fake binary")
async def test_llamacpp_backend_process_sse_and_tools(tmp_path):
    args_file = tmp_path / "llama-args.json"
    _fake(tmp_path, "llama.cpp", "llama-server", FAKE_LLAMA, args_file)
    s = make_settings(tmp_path, models={"backend_mode": "real"})
    s.paths.ensure()
    be = LlamaCppBackend(s, s.paths.runtime, s.paths.logs)
    assert be.available and be.has_flag("--n-cpu-moe") and be.has_flag("--kv-unified")
    spec = Catalog.load().get("qwen3-30b-a3b-instruct")
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF")
    plan = LaunchPlan(spec.id, True, 999, 20, 32768, 4, "q8_0", 9000, 6000)
    inst = await be.start(spec, {"model": model}, plan)
    try:
        argv = json.loads(args_file.read_text())
        assert argv[argv.index("--n-cpu-moe") + 1] == "20"
        assert ["-fa", "on"] == argv[argv.index("-fa"):argv.index("-fa") + 2]
        assert argv[argv.index("--cache-type-k") + 1] == "q8_0" and "--kv-unified" in argv and "--jinja" in argv
        assert argv[argv.index("-c") + 1] == "32768" and argv[argv.index("-np") + 1] == "4"
        events = [e async for e in be.chat(inst, spec, ChatRequest(messages=[{"role": "user", "content": "hi"}]))]
        text = "".join(e.text for e in events if e.type == "content")
        assert text == "こんにちは、世界"
        assert any(e.type == "reasoning" for e in events)
        done = events[-1]
        assert done.type == "done" and done.usage["completion_tokens"] == 3 and done.timings["predicted_per_second"] == 55.5
        tools = [{"type": "function", "function": {"name": "run_code", "parameters": {}}}]
        events = [e async for e in be.chat(inst, spec, ChatRequest(messages=[{"role": "user", "content": "x"}], tools=tools))]
        calls = next(e for e in events if e.type == "tool_calls").tool_calls
        assert calls == [{"id": "c1", "name": "run_code", "arguments": '{"code": "print(1)"}'}]
        assert (await be.embed(inst, ["ab", "abcd"])) == [[2.0, 1.0], [4.0, 1.0]]
        assert be.alive(inst)
    finally:
        await be.stop(inst)
    assert not be.alive(inst)


@pytest.mark.skipif(os.name == "nt", reason="uses a shebang script as fake binary")
async def test_sdcpp_backend_image_and_video(tmp_path):
    import threading

    args_file = tmp_path / "sd-args.txt"
    _fake(tmp_path, "sd.cpp", "sd", FAKE_SD, args_file)
    s = make_settings(tmp_path, models={"backend_mode": "real"})
    s.paths.ensure()
    be = SdCppBackend(s, s.paths.runtime, s.paths.logs)
    assert be.available
    cat = Catalog.load()
    img = cat.get("flux1-schnell")
    paths = {c: tmp_path / f"{c}.bin" for c in img.components}
    progress = []
    outs = await be.generate(img, paths, LaunchPlan(img.id, True, 0, 0, 0, 1, "", 9000, 0, offload=True),
                             {"prompt": "-a cat {x}", "width": 512, "height": 512, "steps": 4, "seed": 7},
                             tmp_path / "o1", lambda v, m: progress.append(v), threading.Event())
    argv = args_file.read_text().splitlines()
    assert argv[argv.index("-p") + 1] == "-a cat {x}"
    assert "--clip-on-cpu" in argv and "--offload-to-cpu" in argv and argv[argv.index("--steps") + 1] == "4"
    assert outs[0].name == "out.png" and max(progress) > 0.5
    vid = cat.get("wan21-t2v-1.3b")
    outs = await be.generate(vid, {c: tmp_path / f"{c}.bin" for c in vid.components}, None,
                             {"prompt": "waves", "width": 480, "height": 272, "frames": 17, "steps": 4, "seed": 1, "fps": 8},
                             tmp_path / "o2", lambda v, m: None, threading.Event())
    assert outs[0].suffix == ".webp"
    anim = Image.open(outs[0])
    assert getattr(anim, "n_frames", 1) == 3


def test_mjpeg_extraction(tmp_path):
    p = tmp_path / "x.avi"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG")
    p.write_bytes(b"hdr" + buf.getvalue() + b"junk" + buf.getvalue())
    assert len(extract_mjpeg_frames(p)) == 2


@pytest.mark.skipif(os.name == "nt", reason="uses a shebang script as fake binary")
def test_platform_real_mode_end_to_end(tmp_path):
    from fastapi.testclient import TestClient

    from nextai.app import create_app
    from nextai.install.models import mark_installed
    from nextai.platform import Platform
    from nextai.resources.monitor import MockGpuProvider

    from conftest import create_member, wait_job, web_login

    _fake(tmp_path, "llama.cpp", "llama-server", FAKE_LLAMA, tmp_path / "args.json")
    s = make_settings(tmp_path, models={"backend_mode": "real", "resident_fast_model": False})
    p = offline_web(Platform(s, gpu=MockGpuProvider()))
    mdir = s.paths.models / "qwen3-4b-instruct" / "model"
    mdir.mkdir(parents=True)
    (mdir / "Qwen3-4B-Q4_K_M.gguf").write_bytes(b"GGUF" * 1000)
    mark_installed(p.db, "qwen3-4b-instruct", {"model": "qwen3-4b-instruct/model/Qwen3-4B-Q4_K_M.gguf"}, 4000)
    assert p.models.usable("qwen3-4b-instruct") and not p.models.usable("qwen3-30b-a3b-instruct")
    with TestClient(create_app(p), base_url="https://testserver") as c:
        create_member(p)
        web_login(c, "alice")
        r = c.post("/api/conversations/new/messages", json={"content": "こんにちは"})
        job = wait_job(c, r.json()["job"]["id"])
        assert job["status"] == "done", job
        conv = c.get(f"/api/conversations/{r.json()['conversation_id']}").json()
        assert conv["messages"][-1]["content"] == "こんにちは、世界"
        assert conv["messages"][-1]["meta"]["model_id"] == "qwen3-4b-instruct"
        caps = c.get("/api/status").json()["capabilities"]
        assert caps["chat"] and not caps["image"]
        # OpenAI-compatible API against the real (llama-server) backend
        key = c.post("/api/account/api-keys", json={"name": "t"}).json()["key"]
        h = {"Authorization": f"Bearer {key}"}
        assert [m["id"] for m in c.get("/v1/models", headers=h).json()["data"]] == ["auto", "qwen3-4b-instruct"]
        r = c.post("/v1/chat/completions", headers=h, json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "こんにちは、世界"
        r = c.post("/v1/chat/completions", headers=h, json={"messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert "こんにちは" in r.text and r.text.rstrip().endswith("data: [DONE]")


def test_cuda_build_selection_and_upgrade(tmp_path, monkeypatch):
    from nextai.install import runtime as rt_mod

    monkeypatch.setattr(rt_mod, "IS_WIN", True)
    assets = [{"name": n, "browser_download_url": "x"} for n in (
        "llama-b11193-bin-win-cpu-x64.zip", "llama-b11193-bin-win-vulkan-x64.zip",
        "llama-b11193-bin-win-cuda-12.4-x64.zip", "llama-b11193-bin-win-cuda-13.4-x64.zip",
        "cudart-llama-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip")]
    accel, main, cudart = rt_mod.pick_llama_assets(assets, "13.4")
    assert accel == "cuda-13.4" and main["name"].endswith("cuda-13.4-x64.zip") and cudart["name"].startswith("cudart")
    assert rt_mod.pick_llama_assets(assets, "12.8")[0] == "cuda-12.4"
    assert rt_mod.pick_llama_assets(assets, None)[0] == "vulkan"

    s = make_settings(tmp_path)
    inst = rt_mod.RuntimeInstaller(s, lambda ev: None)
    inst.manifest["llama.cpp"] = {"version": "b11193", "accel": "vulkan", "exe": "x"}
    monkeypatch.setattr(rt_mod, "driver_cuda_version", lambda: "13.4")
    assert inst.needs_upgrade("llama.cpp")
    inst.manifest["llama.cpp"]["cuda_failed"] = {"version": "b11193"}
    assert not inst.needs_upgrade("llama.cpp")
    inst.manifest["llama.cpp"] = {"version": "b11193", "accel": "cuda-13.4", "exe": "x"}
    assert not inst.needs_upgrade("llama.cpp")
    monkeypatch.setattr(rt_mod, "driver_cuda_version", lambda: None)
    inst.manifest["llama.cpp"] = {"version": "b11193", "accel": "vulkan", "exe": "x"}
    assert not inst.needs_upgrade("llama.cpp")
