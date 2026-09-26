from conftest import ADMIN_PW, MEMBER_PW, admin_token, create_admin, create_member, new_client, web_login

from nextai.auth.deps import cookie_names


def test_login_session_and_csrf(client, platform):
    create_member(platform)
    r = client.post("/api/auth/login", json={"username": "alice", "password": MEMBER_PW})
    assert r.status_code == 403  # missing X-Requested-With (login CSRF)
    data = web_login(client, "alice")
    assert data["user"]["role"] == "member"
    sid = cookie_names(platform.settings)[0]
    assert sid.startswith("__Host-")
    assert client.get("/api/auth/session").status_code == 200
    csrf = client.headers.pop("X-CSRF-Token")
    r = client.post("/api/conversations", json={"title": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/api/conversations", json={"title": "x"}).status_code == 200


def test_wrong_password_and_lockout(client, platform):
    create_member(platform)
    for _ in range(5):
        r = client.post("/api/auth/login", json={"username": "alice", "password": "bad-password-1"},
                        headers={"X-Requested-With": "nextai"})
        assert r.status_code in (401, 429)
    r = client.post("/api/auth/login", json={"username": "alice", "password": MEMBER_PW},
                    headers={"X-Requested-With": "nextai"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_first_login_password_change_gate(client, platform):
    create_member(platform, must_change=True)
    data = web_login(client, "alice")
    assert data["must_change_password"] is True
    assert client.get("/api/conversations").json()["error"]["code"] == "password_change_required"
    r = client.post("/api/auth/password", json={"current_password": MEMBER_PW, "new_password": "short"})
    assert r.status_code == 400
    r = client.post("/api/auth/password", json={"current_password": MEMBER_PW, "new_password": "Brand-New-Pass-42"})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.get("/api/conversations").status_code == 200


def test_trusted_device_refresh_rotation_and_reuse_detection(client, platform):
    create_member(platform)
    web_login(client, "alice", trust=True)
    sid, dev = cookie_names(platform.settings)
    old_dev = client.cookies.get(dev)
    assert old_dev
    client.cookies.delete(sid)
    assert client.get("/api/auth/session").status_code == 401
    r = client.post("/api/auth/refresh", headers={"X-Requested-With": "nextai"})
    assert r.status_code == 200, r.text
    new_dev = client.cookies.get(dev)
    assert new_dev and new_dev != old_dev
    # replay of the rotated token after the grace window => device revoked (theft detection)
    platform.settings.set_override("auth.device_rotation_grace_seconds", 0)
    thief = new_client(platform)
    thief.cookies.set(dev, old_dev, domain="testserver.local")
    thief.cookies.set(dev, old_dev)
    r = thief.post("/api/auth/refresh", headers={"X-Requested-With": "nextai"})
    assert r.status_code == 401
    devices = platform.auth.list_devices(platform.auth.get_user_by_name("alice")["id"])
    assert devices[0]["status"] == "revoked"
    client.cookies.delete(sid)
    assert client.post("/api/auth/refresh", headers={"X-Requested-With": "nextai"}).status_code == 401


def test_device_listing_and_self_revoke(client, platform):
    create_member(platform)
    web_login(client, "alice", trust=True)
    devs = client.get("/api/account/devices").json()["devices"]
    assert len(devs) == 1 and devs[0]["current"]
    assert client.delete(f"/api/account/devices/{devs[0]['id']}").status_code == 200
    assert client.get("/api/auth/session").status_code == 401  # session bound to the revoked device


def test_member_cannot_use_admin_api(client, platform):
    create_member(platform)
    web_login(client, "alice")
    assert client.get("/api/admin/users").status_code == 403
    r = client.post("/api/auth/login", json={"username": "alice", "password": MEMBER_PW, "client": "admin_app"})
    assert r.status_code == 403


def test_admin_cookie_session_rejected_for_admin_api(client, platform):
    create_admin(platform)
    web_login(client, "admin", ADMIN_PW)
    r = client.get("/api/admin/users")
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_app_only"


def test_admin_member_lifecycle(client, platform):
    create_admin(platform)
    h = admin_token(client)
    r = client.post("/api/admin/users", json={"username": "bob", "display_name": "Bob", "storage_quota_mb": 100}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["initial_password"] and "bob" in body["invitation"]
    bob_id = body["user"]["id"]
    m = new_client(platform)
    data = web_login(m, "bob", body["initial_password"])
    assert data["must_change_password"]
    # suspend -> sessions revoked + login blocked
    assert client.post(f"/api/admin/users/{bob_id}/state", json={"state": "suspended"}, headers=h).status_code == 200
    assert m.get("/api/auth/session").status_code == 401
    r = m.post("/api/auth/login", json={"username": "bob", "password": body["initial_password"]},
               headers={"X-Requested-With": "nextai"})
    assert r.status_code == 403
    # reactivate + reset password
    assert client.post(f"/api/admin/users/{bob_id}/state", json={"state": "active"}, headers=h).status_code == 200
    r = client.post(f"/api/admin/users/{bob_id}/reset-password", json={}, headers=h)
    new_pw = r.json()["password"]
    web_login(m, "bob", new_pw)
    # quota change
    r = client.patch(f"/api/admin/users/{bob_id}", json={"concurrent_jobs": 3, "queue_priority": 1}, headers=h)
    assert r.json()["user"]["concurrent_jobs"] == 3
    # member cannot change own role/quota
    r = m.patch("/api/account/profile", json={"display_name": "B"})
    assert r.status_code in (200, 403)
    # delete requires disable first, and confirmation
    r = client.request("DELETE", f"/api/admin/users/{bob_id}", json={"confirm_username": "bob"}, headers=h)
    assert r.status_code == 409
    client.post(f"/api/admin/users/{bob_id}/state", json={"state": "disabled"}, headers=h)
    r = client.request("DELETE", f"/api/admin/users/{bob_id}", json={"confirm_username": "wrong"}, headers=h)
    assert r.status_code == 400
    r = client.request("DELETE", f"/api/admin/users/{bob_id}", json={"confirm_username": "bob"}, headers=h)
    assert r.status_code == 200
    assert all(u["username"] != "bob" for u in client.get("/api/admin/users", headers=h).json()["users"])
    actions = {e["action"] for e in client.get("/api/admin/audit", headers=h).json()["entries"]}
    assert {"user.create", "user.state.suspended", "user.password_reset", "user.delete"} <= actions


def test_last_admin_protection(client, platform):
    admin = create_admin(platform)
    h = admin_token(client)
    r = client.post(f"/api/admin/users/{admin['id']}/state", json={"state": "suspended"}, headers=h)
    assert r.status_code == 409


def test_password_change_revokes_other_sessions(client, platform):
    create_member(platform)
    web_login(client, "alice")
    other = new_client(platform)
    web_login(other, "alice")
    r = client.post("/api/auth/password", json={"current_password": MEMBER_PW, "new_password": "Another-Pass-9876",
                                                "revoke_other_sessions": True})
    assert r.status_code == 200
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert other.get("/api/auth/session").status_code == 401
    assert client.get("/api/auth/session").status_code == 200


def test_profile_update_allowed_fields(client, platform):
    create_member(platform)
    web_login(client, "alice")
    r = client.patch("/api/account/profile", json={"display_name": "アリス", "bio": "hi", "ui_prefs": {"theme": "dark"}})
    assert r.status_code == 200
    assert r.json()["user"]["display_name"] == "アリス"
    r = client.patch("/api/account/profile", json={"role": "admin"})
    assert r.status_code in (200, 422)
    assert platform.auth.get_user_by_name("alice")["role"] == "member"


def test_admin_api_rejects_proxied_requests(client, platform):
    create_admin(platform)
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW, "client": "admin_app"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_local_only"
    h = admin_token(client)
    assert client.get("/api/admin/users", headers=h).status_code == 200
    r = client.get("/api/admin/users", headers={**h, "CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 403


def test_update_check_follows_redirect_and_compares_versions(client, platform):
    import http.server
    import json as _json
    import threading

    manifest = {"version": "9.0.0", "url": "https://example.com/NextAI-Platform-Setup.exe", "sha256": "a" * 64}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/latest":
                self.send_response(302)
                self.send_header("Location", "/v9/update-manifest.json")
                self.end_headers()
                return
            body = _json.dumps(manifest).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        create_admin(platform)
        h = admin_token(client)
        platform.settings.set_override("server.update_manifest_url", f"http://127.0.0.1:{srv.server_address[1]}/latest")
        r = client.get("/api/admin/update/check", headers=h).json()
        assert r["update_available"] and r["latest"] == "9.0.0" and r["sha256"] == "a" * 64
        manifest["version"] = platform.version
        assert client.get("/api/admin/update/check", headers=h).json()["update_available"] is False
    finally:
        srv.shutdown()
