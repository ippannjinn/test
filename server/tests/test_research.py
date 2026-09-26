from conftest import create_member, wait_job, web_login

from nextai.services.research import looks_like_lookup, make_queries
from nextai.tools.web import parse_bing, parse_duckduckgo_lite

PAGES = {
    "https://example.com/trickcal": "トリッカル\n\nトリッカル (Trickcal) は韓国の Epid Games が開発した、かわいいキャラクターが登場する育成RPGです。"
                                    "日本版は2024年に配信され、スマートフォン向けに提供されています。\n\n関係ない段落がここに続きます。" * 2,
    "https://ja.wikipedia.org/wiki/x": "無関係な記事です。天気や料理の話題が書かれています。",
}


def _fake_web(platform, monkeypatch, hits=True):
    calls = {"search": [], "fetch": []}

    async def search(q, n=6):
        calls["search"].append(q)
        if not hits:
            return []
        return [{"title": "トリッカル - 公式", "url": "https://example.com/trickcal", "snippet": "育成RPG トリッカル"},
                {"title": "別の記事", "url": "https://ja.wikipedia.org/wiki/x", "snippet": "天気"}]

    async def fetch_text(url, max_chars=12000):
        calls["fetch"].append(url)
        return {"url": url, "status": 200, "title": "", "text": PAGES.get(url, ""), "links": []}

    monkeypatch.setattr(platform.web, "search", search)
    monkeypatch.setattr(platform.web, "fetch_text", fetch_text)
    return calls


def test_lookup_detection_and_queries():
    assert looks_like_lookup("トリッカルについて教えて")
    assert looks_like_lookup("「葬送のフリーレン」って何？")
    assert not looks_like_lookup("こんにちは")
    assert not looks_like_lookup("この文章を短くして")
    qs = make_queries("トリッカルについて教えてください")
    assert qs[0] == "トリッカル" and "トリッカル とは" in qs


def test_auto_research_answers_from_evidence(client, platform, monkeypatch):
    calls = _fake_web(platform, monkeypatch)
    create_member(platform, "asker")
    web_login(client, "asker")
    r = client.post("/api/conversations/new/messages", json={"content": "トリッカルについて教えて"}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    assert job["status"] == "done", job
    msg = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]
    assert calls["search"] and "https://example.com/trickcal" in calls["fetch"]
    assert msg["meta"]["sources"][0]["url"] == "https://example.com/trickcal"
    assert "web_research" in msg["meta"]["tools"]
    events = client.get(f"/api/jobs/{r['job']['id']}").json()


def test_research_tool_reports_nothing_found(client, platform, monkeypatch):
    _fake_web(platform, monkeypatch, hits=False)
    create_member(platform, "asker2")
    web_login(client, "asker2")
    r = client.post("/api/conversations/new/messages",
                    json={"content": '調べて [[tool:web_research {"question": "存在しないもの"}]]'}).json()
    job = wait_job(client, r["job"]["id"], timeout=30)
    msg = client.get(f"/api/conversations/{r['conversation_id']}").json()["messages"][-1]
    assert job["status"] == "done" and "見つかりません" in msg["content"]


def test_provider_parsers():
    lite = ("<a rel=\"nofollow\" href=\"//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fp\" class='result-link'>A <b>t</b></a>"
            "<td class='result-snippet'>snippet A</td>")
    assert parse_duckduckgo_lite(lite) == [{"title": "A t", "url": "https://a.example/p", "snippet": "snippet A"}]
    assert parse_bing('<li class="b_algo"><h2><a href="https://b.example/">B</a></h2><p>sn</p></li>')[0]["url"] == "https://b.example/"


def test_metasearch_fuses_providers_and_survives_failures(platform, monkeypatch):  # providers stubbed below
    import asyncio

    w = platform.web

    async def ddg(q):
        raise RuntimeError("blocked")

    async def bing(q):
        return [{"title": "A", "url": "https://www.a.example/x/", "snippet": "s"}, {"title": "B", "url": "https://b.example/", "snippet": ""}]

    async def wiki(q):
        return [{"title": "A wiki", "url": "https://a.example/x", "snippet": "longer snippet here"}]

    monkeypatch.setattr(w, "_duckduckgo", ddg)
    monkeypatch.setattr(w, "_bing", bing)
    monkeypatch.setattr(w, "_wikipedia", wiki)
    res = asyncio.run(w.search("q", 5))
    assert [r["url"] for r in res][0] == "https://www.a.example/x/"  # same page from two providers ranks first
    assert res[0]["providers"] == ["bing", "wikipedia"] and res[0]["snippet"] == "longer snippet here"
