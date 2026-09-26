import json

from conftest import admin_token, create_admin, create_member, new_client, web_login


def _key(client, platform, **body) -> str:
    create_member(platform)
    web_login(client, "alice")
    r = client.post("/api/account/api-keys", json={"name": "script", "days": 30, **body})
    assert r.status_code == 200, r.text
    assert r.json()["key"].startswith("nxt_") and r.json()["token"]["scopes"] == ["openai"]
    return r.json()["key"]


def _sse(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(line[6:] if line[6:] == "[DONE]" else json.loads(line[6:]))
    return out


def test_models_and_chat_completion(client, platform):
    key = _key(client, platform)
    c = new_client(platform)
    h = {"Authorization": f"Bearer {key}"}
    ids = [m["id"] for m in c.get("/v1/models", headers=h).json()["data"]]
    assert ids[0] == "auto" and len(ids) > 1
    r = c.post("/v1/chat/completions", headers=h, json={
        "model": "auto", "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "こんにちは"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion" and body["choices"][0]["finish_reason"] == "stop"
    assert "こんにちは" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] > 0 and body["model"] in ids
    # a pinned model is honoured
    llm = next(m["id"] for m in c.get("/v1/models", headers=h).json()["data"] if m.get("kind") == "llm")
    r = c.post("/v1/chat/completions", headers=h, json={"model": llm, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["model"] == llm
    assert c.post("/v1/chat/completions", headers=h, json={"model": "nope", "messages": [{"role": "user", "content": "x"}]}
                  ).status_code == 404
    # jobs are recorded for the member (visible in their history / admin queue stats)
    jobs = platform.db.query("SELECT kind, status FROM jobs WHERE kind='api'")
    assert len(jobs) == 2 and all(j["status"] == "done" for j in jobs)


def test_streaming_and_usage(client, platform):
    key = _key(client, platform)
    c = new_client(platform)
    r = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
        "messages": [{"role": "user", "content": "ストリームのテスト"}], "stream": True,
        "stream_options": {"include_usage": True}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _sse(r.text)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events if isinstance(e, dict)]
    text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks if ch["choices"])
    assert "ストリームのテスト" in text
    assert any(ch["choices"] and ch["choices"][0]["finish_reason"] == "stop" for ch in chunks)
    assert chunks[-1]["usage"]["total_tokens"] > 0 and chunks[-1]["choices"] == []


def test_tool_calls_round_trip(client, platform):
    key = _key(client, platform)
    c = new_client(platform)
    h = {"Authorization": f"Bearer {key}"}
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    msgs = [{"role": "user", "content": '天気は? [[tool:get_weather {"city": "Tokyo"}]]'}]
    r = c.post("/v1/chat/completions", headers=h, json={"messages": msgs, "tools": tools})
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather" and json.loads(call["function"]["arguments"]) == {"city": "Tokyo"}
    msgs += [choice["message"], {"role": "tool", "tool_call_id": call["id"], "content": "晴れ 22℃"}]
    r = c.post("/v1/chat/completions", headers=h, json={"messages": msgs, "tools": tools})
    assert r.json()["choices"][0]["finish_reason"] == "stop" and "晴れ" in r.json()["choices"][0]["message"]["content"]
    # streaming variant emits a tool_calls delta
    r = c.post("/v1/chat/completions", headers=h, json={"messages": msgs[:1], "tools": tools, "stream": True})
    chunks = [e for e in _sse(r.text) if isinstance(e, dict) and e["choices"]]
    assert any("tool_calls" in ch["choices"][0]["delta"] for ch in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_embeddings(client, platform):
    key = _key(client, platform)
    r = new_client(platform).post("/v1/embeddings", headers={"Authorization": f"Bearer {key}"},
                                  json={"input": ["りんご", "みかん"], "model": "auto"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 2 and len(data[0]["embedding"]) > 8 and data[1]["index"] == 1


def test_key_scope_isolation_and_admin_controls(client, platform):
    create_admin(platform)
    key = _key(client, platform)
    c = new_client(platform)
    h = {"Authorization": f"Bearer {key}"}
    # an openai key cannot use the member web API, the admin API or manage keys
    assert c.get("/api/conversations", headers=h).status_code == 403
    assert c.get("/api/admin/dashboard", headers=h).status_code == 403
    assert c.post("/api/account/api-keys", headers=h, json={}).status_code == 403
    # session cookies are not accepted by /v1 (no CSRF surface)
    assert client.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer nxt_bogus"}).status_code == 401
    # listing / revoking own keys
    keys = client.get("/api/account/api-keys").json()["keys"]
    assert len(keys) == 1 and keys[0]["status"] == "active"
    assert client.delete(f"/api/account/api-keys/{keys[0]['id']}x").status_code == 404
    # another member cannot see or revoke alice's key
    create_member(platform, "bob")
    bob = new_client(platform)
    web_login(bob, "bob")
    assert bob.get("/api/account/api-keys").json()["keys"] == []
    assert bob.delete(f"/api/account/api-keys/{keys[0]['id']}").status_code == 404
    # admin can disable the API globally
    ah = admin_token(client)
    assert client.put("/api/admin/settings", headers=ah, json={"values": {"api.enabled": False}}).status_code == 200
    assert c.get("/v1/models", headers=h).status_code == 403
    assert client.put("/api/admin/settings", headers=ah, json={"values": {"api.enabled": True}}).status_code == 200
    assert c.get("/v1/models", headers=h).status_code == 200
    assert client.delete(f"/api/account/api-keys/{keys[0]['id']}").status_code == 200
    assert c.get("/v1/models", headers=h).status_code == 401
    # key limit
    assert client.put("/api/admin/settings", headers=ah, json={"values": {"api.max_keys_per_user": 1}}).status_code == 200
    assert client.post("/api/account/api-keys", json={"name": "a"}).status_code == 200
    assert client.post("/api/account/api-keys", json={"name": "b"}).status_code == 409
    assert client.post("/api/account/api-keys", json={"name": "c", "days": 9999}).status_code == 422


def test_suspension_revokes_keys(client, platform):
    create_admin(platform)
    key = _key(client, platform)
    ah = admin_token(client)
    uid = platform.auth.get_user_by_name("alice")["id"]
    client.post(f"/api/admin/users/{uid}/state", json={"state": "suspended"}, headers=ah)
    assert new_client(platform).get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 401
