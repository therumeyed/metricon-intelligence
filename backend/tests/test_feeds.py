"""Custom news feeds (RSS/Atom), redirect unwrapping, and one-story-one-event across channels. Feed XML here is a SYNTHETIC fixture in the documented formats."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.models import Brand, CompetitorEvent, NewsFeed, Source
from app.services import news
from tests.test_serp import FRESH, collect_news_and_process, news_src

NOW = datetime.now(timezone.utc)
ATOM = f"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Google Alert - builders</title>
<entry><title type="html">&lt;b&gt;Carlisle Homes&lt;/b&gt; opens new display village in Tarneit</title>
<link href="https://www.google.com/url?rct=j&amp;sa=t&amp;url=https://news.example/au/carlisle-display&amp;ct=ga&amp;cd=x"/>
<published>{(NOW - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')}</published><content type="html">&lt;b&gt;Carlisle Homes&lt;/b&gt; has opened a new display village in Melbourne, Victoria with 12 homes.</content></entry>
<entry><title>Celebrity mansion sells in Vaucluse</title><link href="https://news.example/au/mansion"/><published>{(NOW - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}</published><content>Not about building.</content></entry>
</feed>"""
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Metricon launches Hampton range in Victoria</title><link>https://news.example/au/metricon-hampton</link><pubDate>Mon, 19 Oct 2026 01:00:00 GMT</pubDate><description>New range for Melbourne homes.</description></item></channel></rss>"""


def test_atom_entries_are_parsed_with_html_stripped_and_google_redirects_unwrapped():
    items = news.parse_feed(ATOM.encode())
    assert items[0]["title"] == "Carlisle Homes opens new display village in Tarneit"
    assert items[0]["link"] == "https://news.example/au/carlisle-display" and items[0]["published"] is not None and "12 homes" in items[0]["snippet"]


def test_rss_items_still_parse():
    assert news.parse_feed(RSS.encode())[0]["title"].startswith("Metricon launches")


def run_collect_with_feed(seeded, body: bytes, monkeypatch, url="https://feeds.example/alerts.xml"):
    seeded.add(NewsFeed(url=url, label="All brands"))
    seeded.commit()
    monkeypatch.setattr(news, "resolve_article", lambda link: (None, None))
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        respx.route(host="news.google.com").mock(return_value=httpx.Response(200, content=b"<rss><channel></channel></rss>"))
        return news.collect(seeded)


def test_feed_items_naming_a_monitored_builder_become_one_event_each_and_off_topic_items_are_excluded(seeded, monkeypatch):
    out = run_collect_with_feed(seeded, ATOM.encode(), monkeypatch)
    assert out["feeds"] == 1 and out["new"] == 1 and out["excluded"] == 1
    ev = seeded.scalar(select(CompetitorEvent))
    carlisle = seeded.scalar(select(Brand).where(Brand.slug == "carlisle-homes"))
    assert ev.brand_id == carlisle.id and ev.origin == "rss" and ev.event_type == "news_mention" and ev.source_url == "https://news.example/au/carlisle-display" and ev.published_at is not None
    feed = seeded.scalar(select(NewsFeed))
    assert feed.last_error is None and feed.items_last_run == 2 and feed.last_fetched_at is not None
    # a second run adds nothing
    seeded.commit()
    with respx.mock:
        respx.get("https://feeds.example/alerts.xml").mock(return_value=httpx.Response(200, content=ATOM.encode()))
        respx.route(host="news.google.com").mock(return_value=httpx.Response(200, content=b"<rss><channel></channel></rss>"))
        again = news.collect(seeded)
    assert again["new"] == 0 and len(list(seeded.scalars(select(CompetitorEvent)))) == 1


def test_a_broken_feed_is_recorded_and_does_not_stop_the_run(seeded, monkeypatch):
    seeded.add(NewsFeed(url="https://feeds.example/dead.xml"))
    seeded.commit()
    with respx.mock:
        respx.get("https://feeds.example/dead.xml").mock(return_value=httpx.Response(500))
        respx.route(host="news.google.com").mock(return_value=httpx.Response(200, content=b"<rss><channel></channel></rss>"))
        out = news.collect(seeded)
    f = seeded.scalar(select(NewsFeed))
    assert f.last_error and "500" in f.last_error and any(e.get("feed") for e in out["errors"])


def test_the_same_story_from_search_news_and_a_feed_is_one_event(seeded, monkeypatch):
    s = news_src(seeded)
    collect_news_and_process(seeded, s, [FRESH], None)  # search-news channel creates the event
    same_story = ATOM.replace("Carlisle Homes</b> opens new display village in Tarneit", "Carlisle Homes</b> opens new display village in Tarneit").encode()
    # make the feed headline identical to the search headline
    body = same_story.replace(b"&lt;b&gt;Carlisle Homes&lt;/b&gt; opens new display village in Tarneit", FRESH.title.encode())
    run_collect_with_feed(seeded, body, monkeypatch)
    assert len([e for e in seeded.scalars(select(CompetitorEvent))]) == 1


def test_search_news_skips_a_story_a_feed_already_recorded(seeded, monkeypatch):
    run_collect_with_feed(seeded, ATOM.encode(), monkeypatch)
    s = news_src(seeded)
    twin = FRESH.__class__(title="Carlisle Homes opens new display village in Tarneit", url="https://other.example/x", domain="other.example", snippet="Carlisle Homes opened a display village.",
                           published_at=NOW - timedelta(days=1))
    r = collect_news_and_process(seeded, s, [twin], None)
    assert r["new_events"] == 0 and len(list(seeded.scalars(select(CompetitorEvent)))) == 1


@pytest.fixture
def open_client(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    get_settings.cache_clear()
    from app.main import create_app

    yield TestClient(create_app())
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()


H = {"X-Requested-With": "metricon-ui"}


def test_admin_adds_a_feed_only_after_it_has_been_read_successfully(open_client, seeded):
    with respx.mock:
        respx.get("https://feeds.example/good.xml").mock(return_value=httpx.Response(200, content=ATOM.encode()))
        respx.get("https://feeds.example/empty.xml").mock(return_value=httpx.Response(200, content=b"<html>not a feed</html>"))
        respx.get("https://feeds.example/down.xml").mock(return_value=httpx.Response(503))
        ok = open_client.post("/api/admin/feeds", json={"url": "https://feeds.example/good.xml", "label": "All brands"}, headers=H)
        assert ok.status_code == 200 and ok.json()["items_found"] == 2
        assert open_client.post("/api/admin/feeds", json={"url": "https://feeds.example/good.xml"}, headers=H).status_code == 400  # duplicate
        assert open_client.post("/api/admin/feeds", json={"url": "https://feeds.example/empty.xml"}, headers=H).status_code == 400
        assert open_client.post("/api/admin/feeds", json={"url": "https://feeds.example/down.xml"}, headers=H).status_code == 400
        assert open_client.post("/api/admin/feeds", json={"url": "ftp://x"}, headers=H).status_code == 400
    assert [f["label"] for f in open_client.get("/api/admin/feeds").json()] == ["All brands"]
    fid = open_client.get("/api/admin/feeds").json()[0]["id"]
    assert open_client.patch(f"/api/admin/feeds/{fid}", json={"active": False}, headers=H).status_code == 200
    assert open_client.delete(f"/api/admin/feeds/{fid}", headers=H).status_code == 200


def test_admin_can_add_a_standing_search_and_it_is_refused_with_operators(open_client, seeded):
    ok = open_client.post("/api/admin/searches", json={"brand_slug": "metricon", "query": "Metricon first home buyer offer", "kind": "offers"}, headers=H)
    assert ok.status_code == 200
    src = seeded.get(Source, ok.json()["id"])
    assert src.kind == "serp_offers" and src.context["search"]["query"] == "Metricon first home buyer offer"
    assert open_client.post("/api/admin/searches", json={"brand_slug": "metricon", "query": "Metricon first home buyer offer", "kind": "offers"}, headers=H).status_code == 400
    assert open_client.post("/api/admin/searches", json={"brand_slug": "metricon", "query": "site:metricon.com.au offers", "kind": "offers"}, headers=H).status_code == 400
    assert open_client.post("/api/admin/searches", json={"brand_slug": "metricon", "query": "x", "kind": "weird"}, headers=H).status_code == 400
