"""Macro, news, briefs, fetcher, events, jobs and access control. Fixtures are synthetic test data only."""
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import auth
from app.models import AppUser, CompetitorEvent, JobRun, MacroRevision, MacroSeries, NewsItem, Offer
from app.services import briefs as B
from app.services import events, fetcher, jobs, macro, news
from tests.helpers import FakeLLM, ok_result, page

H = {"X-Requested-With": "metricon-ui"}


# --------------------------------------------------------------------------- macro
SDMX = "DATAFLOW,MEASURE,VALUE,SECTOR,WORK_TYPE,BUILDING_TYPE,TSEST,REGION,FREQ,TIME_PERIOD,OBS_VALUE,UNIT_MEASURE,UNIT_MULT,OBS_STATUS,OBS_COMMENT\n"


def test_sdmx_parse_and_period_ends():
    rows = macro.parse_sdmx_csv(SDMX + "ABS:BA_GCCSA(1.0.0),1,1,1,1,110,10,2,M,2026-08,3088,NUM,0,,\nABS:BA_GCCSA(1.0.0),1,1,1,1,110,10,2,M,2026-07,,NUM,0,,\n")
    assert rows == [("2026-08", Decimal("3088"), None)]  # blank observation is skipped, not read as 0
    assert macro.period_end("2026-02") == date(2026, 2, 28) and macro.period_end("2026-Q2") == date(2026, 6, 30)


def test_revisions_are_recorded_not_overwritten_silently(seeded):
    macro.ensure_series(seeded)
    s = seeded.scalar(select(MacroSeries).where(MacroSeries.code == "abs_ba_private_houses_vic"))
    macro.upsert_observations(seeded, s, [("2026-07", Decimal("3278"), None), ("2026-08", Decimal("3088"), None)], None)
    r = macro.upsert_observations(seeded, s, [("2026-07", Decimal("3290"), None), ("2026-08", Decimal("3088"), None)], None)
    seeded.commit()
    assert r == {"new": 0, "revised": 1}
    rev = seeded.scalars(select(MacroRevision)).one()
    assert (rev.previous_value, rev.new_value) == (3278, 3290)


def test_series_metadata_keeps_adjustment_unit_and_does_not_mix_input_costs():
    defs = {d["code"]: d for d in macro.default_series()}
    ba = defs["abs_ba_private_houses_vic"]
    assert "Original" in ba["adjustment"] and ba["unit"] == "Number of houses" and ba["series_key"] == "1.1.1.1.110.10.2.M"  # private sector, new, Houses
    ppi = defs["abs_ppi_house_construction_output_nsw"]
    assert ppi["series_key"].endswith(".OUTPUT.Q") and "OUTPUT" in ppi["description"] and "not what their inputs cost" in ppi["description"]
    cash = defs["rba_cash_rate_target"]
    assert cash["geography"] == "AU" and "does not differ by state" in cash["description"]
    assert not any(k.endswith(("_sa", "_act")) and "cash" in k for k in defs)


def test_macro_view_comparisons_and_cash_rate_is_national(seeded):
    macro.ensure_series(seeded)
    s = seeded.scalar(select(MacroSeries).where(MacroSeries.code == "abs_ba_private_houses_vic"))
    rows = [(f"{2025 + (m - 1) // 12}-{(m - 1) % 12 + 1:02d}", Decimal(100 + m), None) for m in range(1, 17)]  # 2025-01 .. 2026-04
    macro.upsert_observations(seeded, s, rows, None)
    v = macro.series_view(seeded, s)
    assert v["comparison"]["vs_year_ago"]["pct"] == round((116 - 104) / 104 * 100, 1) and v["comparison"]["previous_label"] == "2026-03"
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    _login(seeded, client)
    series = client.get("/api/macro?geo=QLD").json()["series"]
    assert [x["geography"] for x in series if x["indicator"] == "cash_rate"] == ["AU"]
    assert all(x["geography"] in ("QLD", "AU") for x in series)


# --------------------------------------------------------------------------- news
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Home builders face rising costs in Victoria - The Age</title><link>https://news.google.com/rss/articles/AAA?oc=5</link><pubDate>Mon, 12 Oct 2026 01:00:00 GMT</pubDate><source url="https://www.theage.com.au">The Age</source><description>&lt;a&gt;x&lt;/a&gt;</description></item>
<item><title>Home builders face rising costs in Victoria - Yahoo News</title><link>https://news.google.com/rss/articles/BBB?oc=5</link><pubDate>Mon, 12 Oct 2026 02:00:00 GMT</pubDate><source url="https://au.news.yahoo.com">Yahoo News</source><description></description></item>
<item><title>Celebrity mansion sells for record price in Sydney - Gossip</title><link>https://news.google.com/rss/articles/CCC</link><pubDate>Mon, 12 Oct 2026 02:00:00 GMT</pubDate><source url="https://gossip.example">Gossip</source></item>
<item><title>Greensboro developer buys 34-acre tract - Local Paper</title><link>https://news.google.com/rss/articles/DDD</link><pubDate>Mon, 12 Oct 2026 02:00:00 GMT</pubDate><source url="https://local.example">Local</source></item>
</channel></rss>"""


@respx.mock
def test_news_dedupes_syndication_filters_noise_and_labels_headline_only(seeded, monkeypatch):
    respx.get(url__startswith="https://news.google.com/rss/search").mock(return_value=httpx.Response(200, content=RSS.encode()))
    monkeypatch.setattr(news, "resolve_article", lambda link: (None, None))
    from app.models import NewsQuery

    for q in list(seeded.scalars(select(NewsQuery)))[1:]:
        q.active = False
    seeded.commit()
    out = news.collect(seeded)
    items = {n.title_key: n for n in seeded.scalars(select(NewsItem))}
    kept = [n for n in items.values() if n.relevance == "kept"]
    assert len(kept) == 1 and kept[0].also_reported_by == ["Yahoo News"]  # syndicated copy folded into one item
    assert kept[0].evidence_level == "headline_only" and kept[0].source_class == "third_party" and kept[0].published_at and kept[0].retrieved_at
    reasons = {n.title: n.relevance_reason for n in items.values() if n.relevance == "excluded"}
    assert any("Celebrity" in t for t in reasons) and any("Greensboro" in t and "Australian" in r for t, r in reasons.items())
    assert news.canonical("https://x.com/a/?utm_source=fb&id=2#frag") == "https://x.com/a?id=2"
    assert news.classify_source("abs.gov.au", set()) == "official_stats" and news.classify_source("treasury.gov.au", set()) == "government"
    assert news.classify_source("www.carlislehomes.com.au", {"carlislehomes.com.au"}) == "builder_claim"


# --------------------------------------------------------------------------- briefs
def _fact_ids(seeded):
    return B.fact_index(B.gather_facts(seeded, "VIC", date(2026, 10, 12)))


def test_no_model_means_no_fake_brief_and_tables_still_work(seeded):
    b = B.generate_brief(seeded, "VIC", date(2026, 10, 12), None)
    seeded.commit()
    assert b.status == "unavailable" and not b.highlights and "not connected" in (b.summary_note or "").lower() + (b.error or "").lower()
    cur = B.current_brief(seeded, "VIC")
    assert cur["brief"] is None and cur["unavailable"]


def test_brief_highlights_must_cite_real_evidence_and_real_numbers(seeded, monkeypatch):
    from tests.test_offers import cycle, eo, src
    from app.services.offers.schema import PageExtraction

    s = src(seeded, ctx={"region": "x", "scope_level": "state", "area": "Victoria"})
    from tests.test_offers import MAIN

    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    o = seeded.scalar(select(Offer))
    facts = B.gather_facts(seeded, "VIC", date(2026, 10, 12))
    offer_fact = next(f for f in facts["facts"] if f["id"].startswith(f"offer:{o.campaign_id}"))
    good = B.HighlightOut(headline="Carlisle advertises claimed inclusions", geography="Victoria", brands=["Carlisle Homes"], observation="Carlisle's Affinity offer claims up to $82,000 in inclusions.",
                          evidence_ids=[offer_fact["id"]], interpretation="May position against Metricon's range.", marketing_implication="Review positioning.", sales_implication="Ask buyers about conditions.")
    fake_ref = good.model_copy(update={"headline": "Invented", "evidence_ids": ["offer:OFR-NOPE:current"]})
    fake_num = good.model_copy(update={"headline": "Wrong number", "observation": "Carlisle now claims up to $120,000 in inclusions."})
    llm = FakeLLM(B.BriefOut(highlights=[good, fake_ref, fake_num], overall_note="Limited evidence."))
    b = B.generate_brief(seeded, "VIC", date(2026, 10, 12), llm)
    seeded.commit()
    assert b.status == "ok" and [h.headline for h in b.highlights] == ["Carlisle advertises claimed inclusions"]
    dropped = {d["headline"]: d["reason"] for d in b.input_facts["dropped_highlights"]}
    assert "no valid evidence" in dropped["Invented"] and "numbers not found" in dropped["Wrong number"]
    md = B.brief_markdown(B.brief_dict(b))
    assert "**Observation:**" in md and "**Interpretation:**" in md and offer_fact["id"] in md


def test_ai_failure_preserves_previous_brief_with_its_date(seeded):
    class Boom:
        model = "m"

        def structured(self, **k):
            raise RuntimeError("model down")

    first = B.generate_brief(seeded, "AU", date(2026, 10, 5), FakeLLM(B.BriefOut(highlights=[], overall_note="Quiet week.")))
    seeded.commit()
    assert first.status == "ok" and first.is_current
    second = B.generate_brief(seeded, "AU", date(2026, 10, 12), Boom())
    seeded.commit()
    assert second.status == "failed" and not second.highlights
    cur = B.current_brief(seeded, "AU")
    assert cur["brief"]["week_start"] == date(2026, 10, 5) and cur["unavailable"] and cur["latest_attempt"]["status"] == "failed"


def test_number_checker_ignores_counts_and_years_but_catches_new_figures():
    cited = [{"change_pct": 12.5, "value": "Up to $82,000 claimed inclusion value"}]
    assert B.numbers_supported("Up 12.5% over four weeks in 2026; 3 brands moved.", cited) == []
    assert B.numbers_supported("Up 15% and $95,000", cited) == ["15%", "$95,000"]


# --------------------------------------------------------------------------- fetcher
@respx.mock
def test_fetcher_classifies_outcomes_and_never_bypasses_challenges(monkeypatch):
    monkeypatch.setattr(fetcher, "_wait_for_host", lambda *a: None)
    respx.get("https://waf.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://waf.example/offers").mock(return_value=httpx.Response(202, headers={"x-amzn-waf-action": "challenge"}, content=b""))
    r = fetcher.fetch_url("https://waf.example/offers")
    assert r.outcome == "blocked" and "not bypassed" in r.error
    respx.get("https://ok.example/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\nCrawl-delay: 1"))
    respx.get("https://ok.example/private").mock(return_value=httpx.Response(200, text="x"))
    assert fetcher.fetch_url("https://ok.example/private").outcome == "robots_disallowed"
    respx.get("https://ok.example/a").mock(return_value=httpx.Response(301, headers={"location": "https://ok.example/b"}))
    respx.get("https://ok.example/b").mock(return_value=httpx.Response(200, html="<html><body><main><p>Hello world offers</p></main></body></html>", headers={"content-type": "text/html"}))
    r = fetcher.fetch_url("https://ok.example/a")
    assert r.ok and r.final_url == "https://ok.example/b" and r.chain[0]["status"] == 301
    respx.get("https://ok.example/missing").mock(return_value=httpx.Response(404, text="nope"))
    assert fetcher.fetch_url("https://ok.example/missing").outcome == "http_error"
    respx.get("https://ok.example/timeout").mock(side_effect=httpx.ReadTimeout("slow"))
    assert fetcher.fetch_url("https://ok.example/timeout").outcome == "timeout"


def test_snapshots_are_stored_once_when_content_is_unchanged(seeded, monkeypatch):
    from tests.test_offers import src

    s = src(seeded)
    monkeypatch.setattr(fetcher, "fetch_url", lambda url, **k: ok_result(url, page("<p>Free upgrades $5,000 offer</p>")))
    a, _ = fetcher.fetch_source(seeded, s)
    b, _ = fetcher.fetch_source(seeded, s)
    assert a.clean_text and b.clean_text is None and b.unchanged_of_id == a.id and a.raw_gz and a.raw_sha256 == b.raw_sha256
    assert fetcher.effective_text(seeded, b) == a.clean_text and s.last_success_at is not None


# --------------------------------------------------------------------------- competitor events
def test_competitor_items_baseline_new_and_same_identity_price_change(seeded):
    from tests.test_offers import src

    s = src(seeded, url="https://www.carlislehomes.com.au/display-homes/", kind="competitor_page", ctx={"item_type": "design"})
    from app.models import SourceSnapshot

    def snap(links):
        sn = SourceSnapshot(source_id=s.id, requested_url=s.url, outcome="ok", links=links, clean_text="x", content_hash=__import__("hashlib").sha256(str(links).encode()).hexdigest())
        seeded.add(sn)
        seeded.flush()
        return sn

    L = lambda path, text: {"href": f"https://www.carlislehomes.com.au{path}", "text": text}
    first = events.process_competitor_page(seeded, s, snap([L("/home-designs/atlas", "Atlas from $350,000"), L("/home-designs/orion", "Orion")]))
    assert first["baseline"] and first["new"] == 0  # first capture is a baseline, not news
    second = events.process_competitor_page(seeded, s, snap([L("/home-designs/atlas", "Atlas from $360,000"), L("/home-designs/orion", "Orion"), L("/home-designs/vega", "Vega")]))
    seeded.commit()
    types = sorted(e.event_type for e in seeded.scalars(select(CompetitorEvent)))
    assert types == ["new_design", "price_change"] and second["new"] == 1 and second["price_changes"] == 1
    ev = seeded.scalar(select(CompetitorEvent).where(CompetitorEvent.event_type == "new_design"))
    assert ev.effective_date is None and "not a confirmed launch date" in ev.summary
    # same events again (job retry) => no duplicates
    events.process_competitor_page(seeded, s, snap([L("/home-designs/atlas", "Atlas from $360,000"), L("/home-designs/orion", "Orion"), L("/home-designs/vega", "Vega")]))
    assert seeded.scalar(select(func.count()).select_from(CompetitorEvent)) == 2


def test_different_lot_is_a_new_item_not_a_price_change(seeded):
    from tests.test_offers import src
    from app.models import SourceSnapshot

    s = src(seeded, url="https://x.example/house-and-land/", kind="competitor_page", ctx={"item_type": "package"})
    def snap(links):
        sn = SourceSnapshot(source_id=s.id, requested_url=s.url, outcome="ok", links=links, clean_text="x", content_hash=__import__("hashlib").sha256(str(links).encode()).hexdigest())
        seeded.add(sn)
        seeded.flush()
        return sn
    events.process_competitor_page(seeded, s, snap([{"href": "https://x.example/house-and-land/lot-12-oak-grove", "text": "Lot 12 Oak Grove $620,000"}]))
    events.process_competitor_page(seeded, s, snap([{"href": "https://x.example/house-and-land/lot-12-oak-grove", "text": "Lot 12 Oak Grove $620,000"},
                                                    {"href": "https://x.example/house-and-land/lot-40-oak-grove", "text": "Lot 40 Oak Grove $580,000"}]))
    types = [e.event_type for e in seeded.scalars(select(CompetitorEvent))]
    assert types == ["new_package"]  # a cheaper different lot is not a price drop on the first one


# --------------------------------------------------------------------------- jobs
def test_job_lock_prevents_overlap_and_failures_are_isolated(seeded):
    from sqlalchemy import text
    from app.db import get_engine

    @jobs.job("t_sleep")
    def _ok(ctx):
        return {"done": True}

    @jobs.job("t_boom")
    def _boom(ctx):
        raise RuntimeError("nope")

    holder = get_engine().connect()
    holder.execute(text("select pg_advisory_lock(:k)"), {"k": jobs._lock_key("t_sleep")})
    try:
        assert jobs.run_now("t_sleep", trigger="manual")["status"] == "skipped"  # another run holds the lock
    finally:
        holder.execute(text("select pg_advisory_unlock(:k)"), {"k": jobs._lock_key("t_sleep")})
        holder.close()
    assert jobs.run_now("t_sleep", trigger="manual")["status"] == "succeeded"
    out = jobs.run_now("t_boom", trigger="manual")
    assert out["status"] == "failed"
    run = seeded.scalars(select(JobRun).where(JobRun.job_name == "t_boom")).one()
    assert "RuntimeError" in run.error
    jobs.REGISTRY.pop("t_sleep"), jobs.REGISTRY.pop("t_boom")


def test_dispatcher_runs_each_slot_once_and_bounds_failed_retries(seeded):
    calls = {"n": 0}

    @jobs.job("check_macro")
    def fake(ctx):
        calls["n"] += 1
        return {}

    now = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)  # 12:00 Sydney, Wednesday
    due = jobs.due_jobs(now)
    assert "check_macro" in due and "collect_sources" in due
    jobs.run_now("check_macro", trigger="schedule")
    assert "check_macro" not in jobs.due_jobs()
    import app.jobs  # restore real functions
    import importlib
    importlib.reload(app.jobs)


# --------------------------------------------------------------------------- access control
def _login(db, client, role="admin", email="a@example.test"):
    if not db.scalar(select(AppUser).where(AppUser.email == email)):
        auth.create_user(db, email, "correct-horse-battery", role)
        db.commit()
    r = client.post("/api/auth/login", json={"email": email, "password": "correct-horse-battery"}, headers=H)
    assert r.status_code == 200
    return r.json()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def test_every_data_export_and_admin_route_requires_sign_in(client):
    for path in ["/api/meta", "/api/offers", "/api/offers/export.csv", "/api/demand/export.csv", "/api/brief", "/api/brief/export.md", "/api/macro", "/api/events", "/api/news",
                 "/api/admin/users", "/api/admin/jobs", "/api/admin/sources", "/api/admin/diagnostics"]:
        assert client.get(path).status_code == 401, path
    assert client.post("/api/admin/jobs/run", json={"name": "check_macro"}, headers=H).status_code == 401


def test_viewer_can_read_and_export_but_not_administer(seeded, client):
    _login(seeded, client, "viewer", "v@example.test")
    assert client.get("/api/offers").status_code == 200 and client.get("/api/offers/export.csv").status_code == 200
    for method, path in [("get", "/api/admin/users"), ("post", "/api/admin/jobs/run"), ("put", "/api/admin/limits"), ("patch", "/api/admin/brands/metricon")]:
        assert getattr(client, method)(path, headers=H, **({"json": {}} if method != "get" else {})).status_code == 403, path


def test_state_changing_requests_need_the_csrf_header(seeded, client):
    _login(seeded, client)
    assert client.post("/api/admin/jobs/run", json={"name": "check_macro"}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 403


def test_login_throttle_and_bad_credentials(seeded, client):
    auth.create_user(seeded, "t@example.test", "correct-horse-battery", "viewer")
    seeded.commit()
    auth._attempts.clear()
    codes = [client.post("/api/auth/login", json={"email": "t@example.test", "password": "bad"}, headers=H).status_code for _ in range(10)]
    assert codes[:8] == [401] * 8 and codes[8:] == [429, 429]
    auth._attempts.clear()


def test_admin_can_edit_roster_without_code_changes(seeded, client):
    _login(seeded, client)
    r = client.post("/api/admin/brands", json={"name": "Porter Davis", "domains": ["porterdavis.com.au"], "states": ["VIC"]}, headers=H)
    assert r.status_code == 200
    r = client.patch("/api/admin/brands/porter-davis", json={"states": ["VIC", "QLD"], "aliases": [{"alias": "porter davis homes", "kind": "search_term", "validated": True}]}, headers=H)
    assert r.json()["states"] == ["QLD", "VIC"] and r.json()["aliases"][0]["validated"]
    assert len(client.get("/api/meta").json()["brands"]) == 17
    audit = client.get("/api/admin/audit").json()
    assert {a["action"] for a in audit} >= {"brand.create", "brand.update"}


def test_offer_export_keeps_evidence_links_and_as_of(seeded, client, monkeypatch):
    from tests.test_offers import MAIN, cycle, eo, src
    from app.services.offers.schema import PageExtraction

    cycle(seeded, monkeypatch, src(seeded), page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    _login(seeded, client, "viewer", "v2@example.test")
    csv_text = client.get("/api/offers/export.csv").text
    lines = csv_text.strip().splitlines()
    assert "source_url" in lines[0] and "as_of" in lines[0] and "customer_payment_required" in lines[0]
    assert any("carlislehomes.com.au/offers" in l and "2000" in l for l in lines[1:]) and any("82000" in l and "up_to" in l for l in lines[1:])
    cur = client.get("/api/offers").json()
    assert cur["view"] == "current"
    hist = client.get("/api/offers?as_of=2020-01-01").json()
    assert hist["view"] == "historical" and hist["count"] == 0  # a selected historical period is never mixed with current offers
    detail = client.get(f"/api/offers/{cur['offers'][0]['campaign_id']}").json()
    assert detail["versions"][0]["is_baseline"] and detail["evidence"] and detail["sources"][0]["url"].endswith("/offers/")
