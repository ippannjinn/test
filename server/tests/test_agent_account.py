import json
import subprocess
import sys

from conftest import admin_token, create_admin, new_client

from nextai.util import now


def _issue(client, h, **body):
    r = client.post("/api/admin/agent-account", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_agent_account_token_scopes(client, platform):
    create_admin(platform)
    h = admin_token(client)
    res = _issue(client, h, days=7)
    assert res["username"] == "claude" and res["token"].startswith("nxt_") and "NEXTAI_TOKEN=" + res["token"] in res["env"]
    t = {"Authorization": f"Bearer {res['token']}"}
    c = new_client(platform)
    # member API (no CSRF needed for bearer tokens)
    r = c.post("/api/conversations/new/messages", json={"content": "こんにちは"}, headers=t)
    assert r.status_code == 200, r.text
    # admin reads + diagnostics allowed
    for path in ("/api/admin/dashboard", "/api/admin/users", "/api/admin/logs/list", "/api/admin/audit", "/api/admin/queue"):
        assert c.get(path, headers=t).status_code == 200, path
    assert c.post("/api/admin/diagnostics/run", json={"full": False}, headers=t).status_code == 200
    # every admin mutation is refused
    for method, path, body in (("POST", "/api/admin/users", {"username": "evil"}),
                               ("PUT", "/api/admin/settings", {"values": {"auth.password_min_length": 4}}),
                               ("POST", "/api/admin/agent-account", {}),
                               ("POST", "/api/admin/server/restart", {})):
        r = c.request(method, path, json=body, headers=t)
        assert r.status_code == 403 and r.json()["error"]["code"] == "debug_read_only", path
    assert c.post("/api/auth/password", json={"current_password": res["password"], "new_password": "Another-Pass-123"},
                  headers=t).status_code == 403
    # not reachable through a proxy / tunnel
    assert c.get("/api/admin/dashboard", headers={**t, "X-Forwarded-For": "203.0.113.5"}).status_code == 403
    # the UI account works with the issued password
    r = c.post("/api/auth/login", json={"username": "claude", "password": res["password"]}, headers={"X-Requested-With": "nextai"})
    assert r.status_code == 200 and r.json()["user"]["is_agent"] is True and not r.json()["must_change_password"]
    toks = client.get("/api/admin/tokens", headers=h).json()["tokens"]
    assert toks[0]["status"] == "active" and toks[0]["last_used_at"]
    actions = {e["action"] for e in client.get("/api/admin/audit", headers=h).json()["entries"]}
    assert "token.issue" in actions


def test_member_only_token_and_rotation_and_revocation(client, platform):
    create_admin(platform)
    h = admin_token(client)
    first = _issue(client, h, days=1, debug=False)
    t1 = {"Authorization": f"Bearer {first['token']}"}
    assert client.get("/api/conversations", headers=t1).status_code == 200
    assert client.get("/api/admin/dashboard", headers=t1).status_code == 403
    second = _issue(client, h, days=1)
    assert client.get("/api/conversations", headers=t1).status_code == 401  # rotated
    t2 = {"Authorization": f"Bearer {second['token']}"}
    assert client.get("/api/admin/dashboard", headers=t2).status_code == 200
    platform.db.execute("UPDATE api_tokens SET expires_at=? WHERE id=?", (now() - 1, second["token_id"]))
    assert client.get("/api/conversations", headers=t2).status_code == 401  # expired
    third = _issue(client, h, days=1)
    assert client.delete(f"/api/admin/tokens/{third['token_id']}", headers=h).status_code == 200
    assert client.get("/api/conversations", headers={"Authorization": f"Bearer {third['token']}"}).status_code == 401
    fourth = _issue(client, h, days=1)
    uid = platform.auth.get_user_by_name("claude")["id"]
    client.post(f"/api/admin/users/{uid}/state", json={"state": "suspended"}, headers=h)
    assert client.get("/api/conversations", headers={"Authorization": f"Bearer {fourth['token']}"}).status_code == 401


def test_cli_agent_account(tmp_path, settings):
    settings.paths.ensure()
    out = tmp_path / "claude.env"
    r = subprocess.run([sys.executable, "-m", "nextai", "--data-dir", str(settings.paths.data_dir), "agent-account",
                        "--days", "3", "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["scopes"] == ["member", "debug"]
    env = out.read_text(encoding="utf-8")
    assert "NEXTAI_TOKEN=nxt_" in env and "NEXTAI_UI_USER=claude" in env
