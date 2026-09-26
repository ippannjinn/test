from fastapi.testclient import TestClient

from conftest import ADMIN_PW, create_admin, create_member, make_settings, web_login
from nextai.app import create_app
from nextai.platform import Platform
from nextai.resources.monitor import MockGpuProvider


def _tunnel_client(tmp_path):
    # TestClient requests arrive on port 443, so tunnel_port=443 simulates the Funnel listener.
    s = make_settings(tmp_path, server={"tunnel_port": 443, "public_url": "https://pc.example.ts.net"})
    p = Platform(s, gpu=MockGpuProvider())
    return p, TestClient(create_app(p, manage_lifecycle=False), base_url="https://testserver")


def test_tunnel_requests_are_remote(tmp_path):
    p, c = _tunnel_client(tmp_path)
    create_admin(p)
    create_member(p)
    # admin console login and admin API are never reachable through the tunnel
    r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW, "client": "admin_app"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_local_only"
    # members log in normally; the real client IP comes from the tunnel's X-Forwarded-For
    c.headers["X-Forwarded-For"] = "1.2.3.4, 198.51.100.7"
    web_login(c, "alice")
    ips = [r["ip"] for r in p.db.query("SELECT ip FROM audit_log WHERE action='auth.login'")]
    assert ips == ["198.51.100.7"]
    # the browser's Origin is the public (ts.net) host
    r = c.patch("/api/account/profile", json={"display_name": "A"}, headers={"Origin": "https://pc.example.ts.net"})
    assert r.status_code == 200, r.text
    r = c.patch("/api/account/profile", json={"display_name": "B"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_direct_requests_unaffected(client, platform):
    create_admin(platform)
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW, "client": "admin_app"})
    assert r.status_code == 200
