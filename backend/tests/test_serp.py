"""Search-results evidence: Google results stored as snapshots, offers read from snippets, news turned into events. Provider replies are SYNTHETIC fixtures."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Brand, CompetitorEvent, Offer, Source, SourceSnapshot
from app.services import spend
from app.services.offers import pipeline
from app.services.offers.schema import Benefit, ExtractedOffer, PageExtraction
from app.services.serp import activity as A
from app.services.serp import collect as C
from app.services.serp import provider as P
from tests.helpers import FakeLLM

OFFICIAL = "https://www.carlislehomes.com.au/offers/affinity-deal"
SNIPPET = "Receive up to $82K in luxury inclusions on selected Affinity designs. Offer ends 30 November 2026."


def item(title, url, snippet="", ts=None, domain=None, publisher=None, **kw):
    return P.SerpItem(title=title, url=url, domain=domain or url.split("/")[2].removeprefix("www."), snippet=snippet,
                      published_at=ts, publisher=publisher, rank=kw.get("rank"))


class FakeSerp:
    """Stands in for DataForSEOSerp. Replies are scripted per query."""

    def __init__(self, replies: dict[str, list[P.SerpItem]] | None = None, fail: set[str] | None = None, cost=0.002):
        self.replies, self.fail, self.cost, self.calls = replies or {}, fail or set(), cost, []

    def locations(self, iso="au"):
        return [{"location_code": 2036, "location_name": "Australia", "location_type": "Country"}, {"location_code": 20999, "location_name": "Victoria,Australia", "location_type": "State"}]

    def search(self, *, query, kind="organic", location_code=None, language_code="en", depth=10, device="desktop"):
        self.calls.append((query, kind, location_code))
        if query in self.fail:
            raise P.ProviderError("HTTP 500", 500, retryable=True)
        return P.SerpResult(query=query, kind=kind, items=list(self.replies.get(query, [])), cost=self.cost, location_code=location_code)


def offers_src(db, slug="carlisle-homes") -> Source:
    b = db.scalar(select(Brand).where(Brand.slug == slug))
    return db.scalar(select(Source).where(Source.brand_id == b.id, Source.kind == "serp_offers").order_by(Source.id))


def run_collect(db, fake, **kw):
    out = C.collect(db, fake, None, **kw)
    db.commit()
    return out


# --------------------------------------------------------------------------- provider parsing
def test_timestamp_is_converted_to_utc_and_unknown_formats_stay_undated():
    assert P.parse_timestamp("2026-10-12 13:21:00 +10:00") == datetime(2026, 10, 12, 3, 21, tzinfo=timezone.utc)
    assert P.parse_timestamp("2026-10-12 03:21:00 +00:00") == datetime(2026, 10, 12, 3, 21, tzinfo=timezone.utc)
    assert P.parse_timestamp("3 days ago") is None and P.parse_timestamp(None) is None


def test_items_are_parsed_per_kind_and_containers_are_flattened():
    raw = [{"type": "organic", "title": "A", "url": "https://a.com/x", "domain": "www.a.com", "description": "d1", "extended_snippet": "d2"},
           {"type": "people_also_ask", "title": "ignored", "url": "https://b.com"},
           {"type": "top_stories", "items": [{"type": "news_search", "title": "N", "url": "https://n.com/1", "domain": "n.com", "snippet": "s", "timestamp": "2026-10-01 00:00:00 +00:00"}]},
           {"type": "organic", "title": "A again", "url": "https://a.com/x"}]
    org = P.parse_items(raw, "organic")
    assert [i.url for i in org] == ["https://a.com/x"] and org[0].snippet == "d1 d2" and org[0].domain == "a.com"
    news = P.parse_items(raw, "news")
    assert {i.url for i in news} == {"https://a.com/x", "https://n.com/1"}


def test_search_operators_are_refused_before_any_request():
    class NoNet(P.DataForSEOSerp):
        def __init__(self):
            pass

        def _post(self, *a, **k):
            raise AssertionError("must not be called")

    with pytest.raises(P.ProviderError):
        NoNet().search(query="site:carlislehomes.com.au offers")


# --------------------------------------------------------------------------- collection
def test_standing_searches_are_seeded_once_per_brand_and_multi_state_brand_gets_state_variants(seeded):
    sources = list(seeded.scalars(select(Source).where(Source.kind.in_(C.SERP_KINDS))))
    queries = {(s.kind, s.context["search"]["query"]) for s in sources}
    assert ("serp_offers", "Metricon offers") in queries and ("serp_offers", "Metricon current promotions") in queries
    assert ("serp_offers", "Metricon offers Victoria") in queries and ("serp_offers", "Metricon offers South Australia") in queries
    assert ("serp_offers", "Arden Homes offers Victoria") not in queries  # single-state brand: no state variant
    assert ("serp_news", "GJ Gardner Homes") in queries
    assert C.seed_search_sources(seeded) == {"search_sources": 0}  # idempotent
    # a state in the query is a search choice, not the offer's applicability
    assert all(s.state is None for s in sources)


def test_collect_stores_official_results_only_and_rank_changes_do_not_create_new_content(seeded):
    s = offers_src(seeded)
    q = s.context["search"]["query"]
    third_party = item("Carlisle Homes reviews", "https://www.productreview.com.au/carlisle", "10% off if you ask nicely")
    official = item("Affinity deal", OFFICIAL, SNIPPET, rank=1)
    out = run_collect(seeded, FakeSerp({q: [third_party, official]}), source_ids=[s.id])
    assert out["ok"] == 1 and out["location"]["from_catalogue"] is True
    snap = seeded.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == s.id))
    assert snap.outcome == "ok" and OFFICIAL in snap.clean_text and "productreview" not in snap.clean_text
    assert snap.context["not_official_domain"] == 1 and snap.fetch_mode == "serp"
    # same results, different order/rank/timestamps -> identical content hash, body not stored twice
    official2 = item("Affinity deal", OFFICIAL, SNIPPET, rank=3, ts=datetime(2026, 10, 1, tzinfo=timezone.utc))
    run_collect(seeded, FakeSerp({q: [official2, third_party]}), source_ids=[s.id])
    snaps = list(seeded.scalars(select(SourceSnapshot).where(SourceSnapshot.source_id == s.id).order_by(SourceSnapshot.id)))
    assert len(snaps) == 2 and snaps[1].content_hash == snaps[0].content_hash and snaps[1].unchanged_of_id == snaps[0].id and snaps[1].clean_text is None


def test_provider_failure_is_recorded_and_other_searches_continue(seeded):
    bad = seeded.scalar(select(Source).where(Source.kind == "serp_offers").order_by(Source.id))
    fake = FakeSerp(fail={bad.context["search"]["query"]})
    out = run_collect(seeded, fake, kinds=("serp_offers",), limit=3)
    assert out["searches"] == 3 and len(out["errors"]) == 1 and out["ok"] == 2
    seeded.refresh(bad)
    assert bad.last_outcome in ("http_error", "network") and bad.consecutive_failures == 1


def test_collection_stops_at_the_request_limit_without_error(seeded, monkeypatch):
    monkeypatch.setattr(C, "get_settings", lambda: type("S", (), {"serp_max_requests_per_job": 2, "store_raw_snapshots": False, "dataforseo_est_cost_per_serp": 0.002})())
    out = run_collect(seeded, FakeSerp())
    assert out["searches"] == 2 and "per-job" in out["budget_message"]


def test_daily_budget_blocks_before_a_search_is_made(seeded):
    spend.record(seeded, "dataforseo", kind="estimated", amount=1.999)
    seeded.commit()
    fake = FakeSerp()
    out = run_collect(seeded, fake)
    assert out["searches"] == 0 and fake.calls == [] and "daily" in out["budget_message"]


# --------------------------------------------------------------------------- offers from snippets
def serp_offer(title="The Better Deal Affinity Range", url=OFFICIAL) -> ExtractedOffer:
    return ExtractedOffer(title=title, detail_url=url, benefits=[Benefit(mechanism_type="upgrade_inclusion_package", label="Luxury inclusions", amount=82000, amount_qualifier="up_to",
                                                                         amount_kind="inclusion_value", evidence="Receive up to $82K in luxury inclusions on selected Affinity designs.")])


def collect_and_read(db, s, items, llm):
    run_collect(db, FakeSerp({s.context["search"]["query"]: items}), source_ids=[s.id])
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == s.id).order_by(SourceSnapshot.id.desc()))
    out = pipeline.process_source(db, s, snap, llm)
    db.commit()
    return snap, out


def test_offer_read_from_a_snippet_is_marked_search_evidence_and_points_at_the_official_result(seeded):
    s = offers_src(seeded)
    _, out = collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET)], FakeLLM(PageExtraction(offers=[serp_offer()])))
    o = seeded.scalar(select(Offer))
    assert out["extracted"] and o.evidence_level == "search_snippet" and o.source_url == OFFICIAL
    assert o.status == "advertised" and "Google results" in o.status_reason and "snippet" in o.status_reason
    assert o.is_baseline is True
    # no geography is asserted by a search that mentioned a state
    assert o.effective["geography"] == []
    d = __import__("app.services.queries", fromlist=["x"]).offer_dict(seeded, o, detail=True)
    assert d["evidence_level"] == "search_snippet" and d["found_via"] == [s.context["search"]["query"]]


def test_a_quote_that_is_not_in_the_snippet_is_rejected(seeded):
    s = offers_src(seeded)
    bad = serp_offer()
    bad.benefits[0].evidence = "Receive up to $500K in luxury inclusions"
    collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET)], FakeLLM(PageExtraction(offers=[bad])))
    o = seeded.scalar(select(Offer))
    assert o is None or all(m.amount is None for m in o.mechanisms)


def test_without_the_model_search_results_are_stored_but_not_read(seeded):
    s = offers_src(seeded)
    snap, out = collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET)], None)
    assert not out["extracted"] and "not connected" in out["error"]
    assert seeded.scalar(select(Offer)) is None and snap.clean_text


def test_disappearing_from_results_is_no_longer_appearing_not_withdrawn(seeded):
    s = offers_src(seeded)
    collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET)], FakeLLM(PageExtraction(offers=[serp_offer()])))
    collect_and_read(seeded, s, [item("About us", "https://www.carlislehomes.com.au/about", "We build homes.")], FakeLLM(PageExtraction(offers=[])))
    o = seeded.scalar(select(Offer))
    assert o.status == "no_longer_observed" and "search results" in o.status_reason and "withdrawn" in o.status_reason
    collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET)], FakeLLM(PageExtraction(offers=[serp_offer()])))
    seeded.refresh(o)
    assert o.status == "advertised"


def test_two_campaigns_in_one_search_are_not_merged(seeded):
    s = offers_src(seeded)
    other = serp_offer("Free site costs", "https://www.carlislehomes.com.au/offers/site-costs")
    other.benefits[0].evidence = "Receive up to $82K in luxury inclusions on selected Affinity designs."
    collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET), item("Site costs", "https://www.carlislehomes.com.au/offers/site-costs", SNIPPET)],
                     FakeLLM(PageExtraction(offers=[serp_offer()])))
    collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET), item("Site costs", "https://www.carlislehomes.com.au/offers/site-costs", SNIPPET + " Hurry, selling fast.")],
                     FakeLLM(PageExtraction(offers=[serp_offer(), other])))
    assert len(list(seeded.scalars(select(Offer)))) == 2


def test_an_offer_also_seen_on_the_real_page_is_page_evidence(seeded):
    s = offers_src(seeded)
    collect_and_read(seeded, s, [item("Affinity deal", OFFICIAL, SNIPPET)], FakeLLM(PageExtraction(offers=[serp_offer()])))
    o = seeded.scalar(select(Offer))
    from app.models import OfferSource

    page_src = seeded.scalar(select(Source).where(Source.brand_id == o.brand_id, Source.kind == "offer_listing"))
    snap = SourceSnapshot(source_id=page_src.id, requested_url=page_src.url, outcome="ok", fetched_at=datetime.now(timezone.utc))
    seeded.add(snap)
    seeded.flush()
    seeded.add(OfferSource(offer_id=o.id, source_id=page_src.id, role="listing", last_seen_snapshot_id=snap.id))
    seeded.flush()
    seeded.refresh(o)
    pipeline.rebuild_derived(seeded, o)
    assert o.evidence_level == "page"


# --------------------------------------------------------------------------- news -> events
def news_src(db, slug="carlisle-homes") -> Source:
    b = db.scalar(select(Brand).where(Brand.slug == slug))
    return db.scalar(select(Source).where(Source.brand_id == b.id, Source.kind == "serp_news"))


NOW = datetime.now(timezone.utc)
FRESH = item("Carlisle Homes opens new display village in Tarneit", "https://news.example/a", "Carlisle Homes has opened a new display village with 12 homes.", NOW - timedelta(days=3), publisher="Example News")
OLD = item("Carlisle Homes wins award", "https://news.example/old", "Carlisle Homes took out an award.", NOW - timedelta(days=200))
OTHER = item("Carlisle council approves bridge", "https://news.example/b", "A different story entirely.", NOW - timedelta(days=1))
UNDATED = item("Carlisle Homes sponsors local club", "https://news.example/c", "Carlisle Homes sponsors a club.", None)


def collect_news_and_process(db, s, items, llm):
    run_collect(db, FakeSerp({s.context["search"]["query"]: items}), source_ids=[s.id])
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == s.id).order_by(SourceSnapshot.id.desc()))
    r = A.process_news_source(db, s, snap, llm)
    db.commit()
    return r


def test_news_without_the_model_becomes_plain_headline_events_and_skips_old_and_off_brand(seeded):
    s = news_src(seeded)
    r = collect_news_and_process(seeded, s, [FRESH, OLD, OTHER, UNDATED], None)
    assert r["new_events"] == 2 and r["skipped_old"] == 1 and r["skipped_not_brand"] == 1
    evs = {e.title: e for e in seeded.scalars(select(CompetitorEvent))}
    e = evs[FRESH.title]
    assert e.event_type == "news_mention" and e.origin == "search_news" and e.source_url == FRESH.url and e.published_at is not None and e.is_baseline is False
    assert evs[UNDATED.title].is_baseline is True  # undated on first capture: background, not news
    # running again does not duplicate
    r2 = collect_news_and_process(seeded, s, [FRESH, OLD, OTHER, UNDATED], None)
    assert r2["new_events"] == 0 and len(list(seeded.scalars(select(CompetitorEvent)))) == 2


def judged(summary="Carlisle Homes opened a new display village with 12 homes.", about=True, evidence="Carlisle Homes has opened a new display village", etype="news_product"):
    return A.NewsJudgements(items=[A.ItemJudgement(index=0, about_brand=about, event_type=etype, summary=summary, evidence=evidence)])


def test_model_judgement_sets_type_and_summary_when_it_matches_the_snippet(seeded):
    s = news_src(seeded)
    collect_news_and_process(seeded, s, [FRESH], FakeLLM(judged()))
    e = seeded.scalar(select(CompetitorEvent))
    assert e.event_type == "news_product" and e.summary.startswith("Carlisle Homes opened") and "checked against" in e.after["note"]


def test_model_summary_with_an_invented_number_falls_back_to_the_snippet(seeded):
    s = news_src(seeded)
    collect_news_and_process(seeded, s, [FRESH], FakeLLM(judged(summary="Carlisle Homes opened a display village with 40 homes.")))
    e = seeded.scalar(select(CompetitorEvent))
    assert e.summary == FRESH.snippet and "did not pass" in e.after["note"]


def test_items_the_model_says_are_not_about_the_builder_are_dropped_and_not_rejudged(seeded):
    s = news_src(seeded)
    llm = FakeLLM(judged(about=False))
    collect_news_and_process(seeded, s, [FRESH], llm)
    assert seeded.scalar(select(CompetitorEvent)) is None
    collect_news_and_process(seeded, s, [FRESH], llm)
    assert llm.calls == 1


def test_model_failure_still_records_headlines_for_that_run(seeded):
    from app.services.llm import LLMError

    def boom(user):
        raise LLMError("refused")

    s = news_src(seeded)
    r = collect_news_and_process(seeded, s, [FRESH], FakeLLM(boom))
    e = seeded.scalar(select(CompetitorEvent))
    assert "ai_error" in r and e.event_type == "news_mention"
