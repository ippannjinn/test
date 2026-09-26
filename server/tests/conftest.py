from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nextai.app import create_app
from nextai.config import Settings
from nextai.platform import Platform
from nextai.resources.monitor import MockGpuProvider

ADMIN_PW = "Kanri-Passw0rd-xyz"
MEMBER_PW = "Member-Passw0rd-xyz"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "server": {"tls": False, "cookie_secure": True},
        "models": {"backend_mode": "mock", "resident_fast_model": False},
        "resources": {"monitor_interval_seconds": 0.2, "ram_elevated_mb": 64, "ram_high_mb": 32, "ram_critical_mb": 16,
                      "disk_margin_gb": 0.01, "disk_low_buffer_gb": 0.0},
        "scheduler": {"tick_seconds": 0.05, "swap_patience_seconds": 0.2},
        "sandbox": {"backend": "process", "timeout_seconds": 5},
    }
    for k, v in overrides.items():
        values.setdefault(k, {}).update(v)
    return Settings(tmp_path / "data", values)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def offline_web(p):
    """Tests never hit real search engines: providers return nothing unless a test stubs them."""
    async def none(_q):
        return []

    for name in ("_duckduckgo", "_bing", "_wikipedia", "_searxng", "_brave"):
        setattr(p.web, name, none)
    return p


@pytest.fixture
def platform(settings):
    return offline_web(Platform(settings, gpu=MockGpuProvider()))


@pytest.fixture
def client(platform):
    app = create_app(platform)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def create_admin(p, username="admin"):
    u, _ = p.auth.create_user(username=username, password=ADMIN_PW, role="admin", must_change_password=False)
    return u


def create_member(p, username="alice", must_change=False, **kw):
    u, _ = p.auth.create_user(username=username, password=MEMBER_PW, role="member", must_change_password=must_change, **kw)
    return u


def web_login(c: TestClient, username: str, password: str = MEMBER_PW, trust: bool = False) -> dict:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "trust_device": trust},
               headers={"X-Requested-With": "nextai"})
    assert r.status_code == 200, r.text
    data = r.json()
    c.headers["X-CSRF-Token"] = data["csrf_token"]
    return data


def admin_token(c: TestClient, username: str = "admin") -> dict:
    r = c.post("/api/auth/login", json={"username": username, "password": ADMIN_PW, "client": "admin_app"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def new_client(platform) -> TestClient:
    return TestClient(create_app(platform, manage_lifecycle=False), base_url="https://testserver")


def wait_job(c: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200, r.text
        j = r.json()["job"]
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.15)
    raise AssertionError("job did not finish")
