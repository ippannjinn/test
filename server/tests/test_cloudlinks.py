"""Google Drive / Docs / Dropbox / OneDrive share links are downloaded as the file itself."""
from conftest import create_member, wait_job, web_login
from test_extools import _fake_releases

from nextai.services.cloudlinks import direct_url
from nextai.tools.web import FetchResult, _disposition_name


def test_share_links_become_direct_downloads():
    fid = "1AbCdEfGhIjKlMnOpQrStUv"
    for u in (f"https://drive.google.com/file/d/{fid}/view?usp=sharing", f"https://drive.google.com/open?id={fid}",
              f"https://drive.google.com/uc?id={fid}&export=download"):
        url, svc = direct_url(u)
        assert svc == "Google Drive" and url.startswith("https://drive.usercontent.google.com/download?")
        assert f"id={fid}" in url and "confirm=t" in url
    url, svc = direct_url(f"https://docs.google.com/spreadsheets/d/{fid}/edit#gid=0")
    assert svc == "Google spreadsheet" and url.endswith(f"/spreadsheets/d/{fid}/export?format=xlsx")
    assert direct_url(f"https://docs.google.com/document/d/{fid}/edit", "pdf")[0].endswith("export?format=pdf")
    url, _ = direct_url(f"https://docs.google.com/spreadsheets/d/{fid}/edit?gid=42", "csv")
    assert url.endswith("format=csv&gid=42")
    url, svc = direct_url("https://www.dropbox.com/scl/fi/abc123/report.pdf?rlkey=xyz&dl=0")
    assert svc == "Dropbox" and url == "https://www.dropbox.com/scl/fi/abc123/report.pdf?rlkey=xyz&dl=1"
    url, svc = direct_url("https://1drv.ms/u/s!AbCdEf")
    assert svc == "OneDrive" and url.startswith("https://api.onedrive.com/v1.0/shares/u!")
    assert direct_url("https://example.com/a.csv") == ("https://example.com/a.csv", "")
    assert direct_url("https://drive.google.com/drive/folders") == ("https://drive.google.com/drive/folders", "")


def test_content_disposition_names():
    assert _disposition_name('attachment; filename="talk.mp4"') == "talk.mp4"
    assert _disposition_name("attachment; filename*=UTF-8''%E4%BC%9A%E8%AD%B0.m4a") == "会議.m4a"
    assert _disposition_name("inline") == ""


def _send(client, text):
    r = client.post("/api/conversations/new/messages", json={"content": text}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    return job, client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]


def test_drive_link_is_downloaded_and_converted(client, platform, monkeypatch, tmp_path):
    _fake_releases(monkeypatch, tmp_path)
    seen = []

    async def fetch(url, max_bytes=None):
        seen.append(url)
        return FetchResult(url, 200, "video/mp4", b"MP4DATA", False, "会議 録画.mp4")

    monkeypatch.setattr(platform.web, "fetch", fetch)
    create_member(platform, "driveuser")
    web_login(client, "driveuser")
    job, msg = _send(client, "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view?usp=sharing これをmp3にして")
    assert job["status"] == "done"
    assert seen and seen[0].startswith("https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOpQrStUv")
    assert [a["name"] for a in msg["meta"]["assets"]] == ["会議_録画.mp3"]


def test_private_drive_link_explains_sharing(client, platform, monkeypatch):
    async def fetch(url, max_bytes=None):
        return FetchResult(url, 200, "text/html; charset=utf-8", b"<html>Sign in</html>", False)

    monkeypatch.setattr(platform.web, "fetch", fetch)
    create_member(platform, "privuser")
    web_login(client, "privuser")
    call = '[[tool:download_file {"url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view"}]]'
    job, msg = _send(client, "このデータを分析して " + call)
    j = platform.jobs.get(job["id"])
    res = [e["data"] for e in j.events if e["type"] == "tool_result" and e["data"]["name"] == "download_file"]
    assert res and not res[0]["ok"] and "リンクを知っている全員" in res[0]["summary"]
