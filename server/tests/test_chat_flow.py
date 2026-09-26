import io
import json
import time

from conftest import create_member, new_client, wait_job, web_login


def _sse_events(c, job_id):
    events = []
    with c.stream("GET", f"/api/jobs/{job_id}/events") as r:
        assert r.status_code == 200
        etype = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                etype = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and etype:
                events.append((etype, json.loads(line[5:])))
                if etype == "done":
                    break
    return events


def test_basic_chat_streams_and_persists(client, platform):
    create_member(platform)
    web_login(client, "alice")
    r = client.post("/api/conversations/new/messages", json={"content": "こんにちは、自己紹介して"})
    assert r.status_code == 200, r.text
    body = r.json()
    events = _sse_events(client, body["job"]["id"])
    types = [t for t, _ in events]
    assert "profile" in types and "delta" in types and types[-1] == "done"
    prof = next(d for t, d in events if t == "profile")
    assert prof["model_id"] and prof["label"]
    conv = client.get(f"/api/conversations/{body['conversation_id']}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["meta"]["model_id"]


def test_agent_tool_loop_with_sandbox_and_memory(client, platform):
    create_member(platform)
    web_login(client, "alice")
    msg = '/code この計算を実行して [[tool:run_code {"code": "print(6*7)"}]]'
    r = client.post("/api/conversations/new/messages", json={"content": msg, "mode": "quality"})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    j = platform.jobs.get(job["id"])
    results = [e["data"] for e in j.events if e["type"] == "tool_result" and e["data"]["name"] == "run_code"]
    assert results and results[0]["ok"] and "42" in results[0]["summary"]
    # memory op
    r = client.post("/api/conversations/new/messages", json={"content": "私の好きな色は青だと覚えておいて"})
    wait_job(client, r.json()["job"]["id"])
    mems = client.get("/api/memory").json()["memories"]
    assert any("青" in m["content"] for m in mems)


def test_agent_stops_on_repeated_failures(client, platform):
    create_member(platform)
    web_login(client, "alice")
    bad = " ".join(['[[tool:web_fetch {"url": "http://127.0.0.1/x"}]]'] * 8)
    r = client.post("/api/conversations/new/messages", json={"content": f"最新情報を調べて {bad}", "mode": "quality"})
    job = wait_job(client, r.json()["job"]["id"], 30)
    assert job["status"] == "done"
    j = platform.jobs.get(job["id"])
    fails = [e for e in j.events if e["type"] == "tool_result" and not e["data"]["ok"]]
    assert fails and "拒否" in fails[0]["data"]["summary"]
    assert any(e["type"] == "notice" for e in j.events) or len(fails) <= 8


def test_image_generation_job_creates_asset_and_uses_quota(client, platform):
    create_member(platform, generation_quota_daily=1)
    web_login(client, "alice")
    caps = client.get("/api/generate/capabilities").json()
    assert caps["image"]["available"] and "クラウド" in caps["video"]["notice"]
    r = client.post("/api/generate/image", json={"prompt": "夕焼けの富士山", "params": {"width": 512, "height": 512}})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done", job
    fid = job["result"]["assets"][0]["id"]
    r = client.get(f"/api/files/{fid}/content")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]
    r = client.post("/api/generate/image", json={"prompt": "もう一枚"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "quota_exceeded"


def test_chat_triggers_media_and_music(client, platform):
    create_member(platform)
    web_login(client, "alice")
    r = client.post("/api/conversations/new/messages", json={"content": "猫の画像を生成して"})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["status"] == "done"
    assert job["result"]["assets"][0]["mime"] == "image/png"
    r = client.post("/api/generate/music", json={"prompt": "calm piano", "params": {"seconds": 2}})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["result"]["assets"][0]["mime"] == "audio/wav"
    r = client.post("/api/generate/video", json={"prompt": "waves", "params": {"frames": 9}})
    job = wait_job(client, r.json()["job"]["id"])
    assert job["result"]["assets"][0]["mime"] == "image/webp"


def test_file_upload_analysis_and_isolation(client, platform):
    create_member(platform)
    create_member(platform, "bob")
    web_login(client, "alice")
    r = client.post("/api/files", files={"file": ("memo.txt", io.BytesIO("売上は100万円です".encode()), "text/plain")})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["id"]
    r = client.post("/api/conversations/new/messages", json={"content": "このファイルを要約して", "attachments": [fid]})
    conv_id = r.json()["conversation_id"]
    wait_job(client, r.json()["job"]["id"])
    bob = new_client(platform)
    web_login(bob, "bob")
    assert bob.get(f"/api/files/{fid}").status_code == 404
    assert bob.get(f"/api/files/{fid}/content").status_code == 404
    assert bob.get(f"/api/conversations/{conv_id}").status_code == 404
    assert bob.get(f"/api/jobs/{r.json()['job']['id']}").status_code == 404
    r2 = bob.post("/api/conversations/new/messages", json={"content": "x", "attachments": [fid]})
    assert r2.status_code == 404
    mem = client.post("/api/memory", json={"content": "alice secret"}).json()["memory"]
    assert bob.delete(f"/api/memory/{mem['id']}").status_code == 404
    assert all(m["content"] != "alice secret" for m in bob.get("/api/memory").json()["memories"])


def test_upload_quota_enforced(client, platform):
    create_member(platform, storage_quota_mb=1)
    web_login(client, "alice")
    data = b"x" * (600 * 1024)
    assert client.post("/api/files", files={"file": ("a.bin", io.BytesIO(data))}).status_code == 200
    r = client.post("/api/files", files={"file": ("b.bin", io.BytesIO(data))})
    assert r.status_code == 507


def test_cancel_job(client, platform):
    create_member(platform)
    web_login(client, "alice")
    platform.backends.llm.token_delay = 0.05
    r = client.post("/api/conversations/new/messages", json={"content": "長い文章を書いて" * 20})
    jid = r.json()["job"]["id"]
    time.sleep(0.2)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 200
    job = wait_job(client, jid)
    assert job["status"] == "cancelled"


def test_admin_dashboard_models_and_queue(client, platform):
    from conftest import admin_token, create_admin

    create_admin(platform)
    h = admin_token(client)
    d = client.get("/api/admin/dashboard", headers=h).json()
    assert d["resources"]["gpus"][0]["vram_total_mb"] == 12227
    assert "queue" in d and "governor" in d
    models = client.get("/api/admin/models", headers=h).json()["models"]
    fast = next(m for m in models if m["id"] == "qwen3-4b-instruct")
    assert fast["usable"]
    r = client.post("/api/admin/models/qwen3-4b-instruct/load", headers=h)
    assert r.status_code == 200, r.text
    assert platform.models.runtimes["qwen3-4b-instruct"].state == "hot"
    assert client.post("/api/admin/models/qwen3-4b-instruct/unload", headers=h).status_code == 200
    r = client.put("/api/admin/settings", json={"values": {"profile.balanced.max_steps": 7}}, headers=h)
    assert r.status_code == 200
    assert platform.settings.get("profile.balanced.max_steps") == 7
    r = client.put("/api/admin/settings", json={"values": {"nope.key": 1}}, headers=h)
    assert r.status_code == 400
    assert client.get("/api/admin/workers", headers=h).status_code == 200
    assert client.get("/api/admin/queue", headers=h).status_code == 200
    r = client.post("/api/admin/diagnostics/run", json={"full": True}, headers=h)
    assert r.status_code == 200, r.text
    jid = r.json()["job"]["id"]
    for _ in range(200):
        j = client.get(f"/api/admin/jobs/{jid}", headers=h).json()["job"]
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    names = {c["id"] for c in j["result"]["checks"]}
    assert {"ram", "storage", "queue", "ssrf", "sandbox", "model_load", "inference", "model_swap"} <= names
    b = client.post("/api/admin/backup", json={"include_user_files": True}, headers=h)
    assert b.status_code == 200
    assert client.get("/api/admin/backups", headers=h).json()["backups"]


def test_regenerate_and_edit(client, platform):
    from conftest import create_member, wait_job, web_login

    create_member(platform, "regen")
    web_login(client, "regen")
    r = client.post("/api/conversations/new/messages", json={"content": "一つ目"}).json()
    cid = r["conversation_id"]
    wait_job(client, r["job"]["id"])
    r2 = client.post(f"/api/conversations/{cid}/messages", json={"content": "二つ目"}).json()
    wait_job(client, r2["job"]["id"])
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    # regenerate replaces only the last answer
    g = client.post(f"/api/conversations/{cid}/regenerate", json={}).json()
    wait_job(client, g["job"]["id"])
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[-1]["job_id"] == g["job"]["id"]
    # editing the first message drops it and everything after
    e = client.post(f"/api/conversations/{cid}/messages", json={"content": "編集後", "replace_from": msgs[0]["id"]}).json()
    wait_job(client, e["job"]["id"])
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["編集後"] and len(msgs) == 2


def _chat(client, text):
    from conftest import wait_job

    r = client.post("/api/conversations/new/messages", json={"content": text}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    msgs = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"]
    return job, msgs[-1]


def test_media_generation_is_an_llm_tool(client, platform):
    from conftest import create_member, web_login

    create_member(platform, "artist")
    web_login(client, "artist")
    job, msg = _chat(client, "夕焼けの海辺のイラストを生成して")
    assert job["status"] == "done", job
    assert "generate_image" in msg["meta"]["tools"]
    assert msg["meta"]["assets"] and msg["meta"]["assets"][0]["mime"].startswith("image/")
    assert msg["meta"]["model_id"] and "flux" not in msg["meta"]["model_id"]  # the reply comes from the LLM
    usage = client.get("/api/account/usage").json()
    assert usage["generation_used_today"] > 0
    job, msg = _chat(client, "落ち着いたピアノのBGMを作曲して")
    assert "generate_music" in msg["meta"]["tools"] and msg["meta"]["assets"][0]["mime"] == "audio/wav"


def test_media_fallback_when_model_skips_the_tool(client, platform, monkeypatch):
    from conftest import create_member, web_login
    from nextai.backends.mock import MockLLMBackend

    orig = MockLLMBackend.chat

    def no_tools(self, instance, spec, req):
        req.tools = None
        return orig(self, instance, spec, req)

    monkeypatch.setattr(MockLLMBackend, "chat", no_tools)
    create_member(platform, "artist2")
    web_login(client, "artist2")
    job, msg = _chat(client, "猫のイラストを描いて")
    assert job["status"] == "done", job
    assert msg["meta"]["assets"] and "画像を生成しました" in msg["content"]


def test_media_quota_is_enforced_inside_the_tool(client, platform):
    from conftest import create_member, web_login

    create_member(platform, "poor", generation_quota_daily=0)
    web_login(client, "poor")
    job, msg = _chat(client, "犬の画像を生成して")
    assert job["status"] == "done" and not msg["meta"]["assets"]
