import json

from conftest import create_member, wait_job, web_login


def _send(client, conv, text, attachments=()):
    r = client.post(f"/api/conversations/{conv}/messages", json={"content": text, "attachments": list(attachments)}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    msg = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]
    return r["conversation_id"], job, msg


def _tool(name, **args):
    return f"[[tool:{name} {json.dumps(args, ensure_ascii=False)}]]"


def test_workspace_persists_and_holds_uploads(client, platform):
    create_member(platform, "coder")
    web_login(client, "coder")
    up = client.post("/api/files", files={"file": ("売上.csv", b"month,sales\n1,10\n2,32\n", "text/csv")}).json()["file"]
    code1 = ("import csv\nrows=list(csv.DictReader(open('uploads/売上.csv',encoding='utf-8')))\n"
             "open('total.csv','w').write('total\\n%d\\n' % sum(int(r['sales']) for r in rows))\n"
             "open('helper.py','w').write('x=1')\nprint('ok', len(rows))")
    conv, job, msg = _send(client, "new", "このCSVを集計して実行して " + _tool("run_code", code=code1), [up["id"]])
    assert job["status"] == "done", job
    names = [a["name"] for a in msg["meta"]["assets"]]
    assert names == ["total.csv"]  # deliverable shown; helper .py and the upload are not
    ws = platform.files.conv_workspace(platform.auth.get_user_by_name("coder")["id"], conv)
    assert (ws / "uploads" / "売上.csv").exists() and (ws / "total.csv").read_text().strip().endswith("42")
    # next turn: files are still there for the sandboxed code
    code2 = "print(open('total.csv').read().split()[-1])"
    conv2, job, msg = _send(client, conv, "前の結果を読んで実行して " + _tool("run_code", code=code2))
    assert conv2 == conv and job["status"] == "done"
    assert "42" in msg["content"] and not msg["meta"]["assets"]  # unchanged file is not shown again
    # deleting the conversation removes its sandbox workspace
    assert client.delete(f"/api/conversations/{conv}").status_code == 200
    assert not ws.exists()


def test_share_file_and_blocked_internal_download(client, platform):
    create_member(platform, "res")
    web_login(client, "res")
    code = "open('script.py','w').write('print(1)')"
    conv, job, msg = _send(client, "new", "スクリプトを書いて実行して " + _tool("run_code", code=code))
    assert not msg["meta"]["assets"]
    conv, job, msg = _send(client, conv, "そのファイルを渡して " + _tool("share_file", path="script.py"))
    assert [a["name"] for a in msg["meta"]["assets"]] == ["script.py"]
    conv, job, msg = _send(client, conv, "Webのデータを調べて " + _tool("download_file", url="http://127.0.0.1:1/x.csv"))
    assert "拒否" in msg["content"] or "内部" in msg["content"] or "ブロック" in msg["content"]


def test_previews_are_sandboxed_and_text_preview(client, platform):
    create_member(platform, "pview")
    web_login(client, "pview")
    html = client.post("/api/files", files={"file": ("page.html", b"<h1>x</h1><script>1</script>", "text/html")}).json()["file"]
    r = client.get(f"/api/files/{html['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp and "frame-ancestors 'self'" in csp
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    # the normal content endpoint still never renders HTML
    r = client.get(f"/api/files/{html['id']}/content")
    assert r.headers["content-type"] == "application/octet-stream" and "attachment" in r.headers["content-disposition"]
    csv = client.post("/api/files", files={"file": ("t.csv", b"a,b\n1,2\n", "text/csv")}).json()["file"]
    d = client.get(f"/api/files/{csv['id']}/text").json()
    assert d["text"].startswith("a,b") and not d["truncated"]
    zipf = client.post("/api/files", files={"file": ("x.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip")}).json()["file"]
    assert client.get(f"/api/files/{zipf['id']}/text").status_code == 415
    assert client.get(f"/api/files/{csv['id']}/preview").status_code == 415
    # ad-hoc previews of code blocks are per-user
    url = client.post("/api/preview", json={"kind": "svg", "content": "<svg xmlns='http://www.w3.org/2000/svg'/>"}).json()["url"]
    assert client.get(url).headers["content-type"] == "image/svg+xml"
    create_member(platform, "other")
    from conftest import new_client
    c2 = new_client(platform)
    web_login(c2, "other")
    assert c2.get(url).status_code == 404


def test_deep_research_mode_and_content_search(client, platform):
    from nextai.profile.analyzer import analyze

    a = analyze("日本の再生可能エネルギーの現状", mode="deep")
    assert a.deep_research and a.task_type == "research" and a.needs_web
    prof = platform.profiles.decide(a)
    assert prof.plan and prof.verify and "web_search" in prof.tools and "download_file" in prof.tools
    assert prof.limits["max_steps"] >= 14
    create_member(platform, "srch")
    web_login(client, "srch")
    r = client.post("/api/conversations/new/messages", json={"content": "こんにちは"}).json()
    wait_job(client, r["job"]["id"])
    client.post(f"/api/conversations/{r['conversation_id']}/messages", json={"content": "珍しい単語ポムポムプリン"}).json()
    hits = client.get("/api/conversations", params={"q": "ポムポム"}).json()["conversations"]
    assert [h["id"] for h in hits] == [r["conversation_id"]]
    assert client.get("/api/conversations", params={"q": "100%_"}).status_code == 200
