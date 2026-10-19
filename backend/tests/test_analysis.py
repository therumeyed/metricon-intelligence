"""AI analysis: facts, validation against the page inventory and cited evidence, and graceful behaviour when the model is absent or fails. Model outputs are SYNTHETIC."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.models import InsightReport, RelatedQueryCandidate, SitePage
from app.services import analysis as AN
from app.services.llm import LLMError
from tests.helpers import FakeLLM
from tests.test_inventory_opportunities import seeded_gsc

WEEK = date(2026, 10, 19)


def add_pages(db, *paths):
    for p in paths:
        db.add(SitePage(url=f"https://www.metricon.com.au{p}", path=p, sources=["sitemap"]))
    db.flush()


def setup_data(seeded, tmp_path):
    iid = seeded_gsc(seeded, tmp_path)
    add_pages(seeded, "/knockdown-rebuild/melbourne", "/granny-flats-info")
    seeded.add(RelatedQueryCandidate(seed="home builders", geo_key="AU", query="granny flat builders", kind="rising", value=250, relevant=True, review_status="proposed"))
    seeded.commit()
    return iid


def rec(**kw):
    d = dict(type="create_page", title="Create a granny flat page", why="Granny flat searches are rising.", action="Publish a guide.", priority="high", effort="medium",
             target_urls=[], topic_terms=["granny flat"], state=None, success_signal="Impressions for granny flat queries", evidence_ids=["new:1"], caveat=None)
    d.update(kw)
    return AN.RecommendationOut(**d)


def out(*recs, **kw):
    d = dict(summary="Summary.", new_in_market=[], themes=[], competitor_moves=[], recommendations=list(recs), data_gaps=[])
    d.update(kw)
    return AN.AnalysisOut(**d)


def run(seeded, o):
    facts = AN.build_facts(seeded, "AU", WEEK)
    return AN.validate(seeded, o, facts), facts


def test_facts_carry_opportunities_new_searches_and_the_inventory(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    facts = AN.build_facts(seeded, "AU", WEEK)
    kinds = {f["kind"] for f in facts["facts"]}
    assert {"gsc_theme", "gsc_quick_win", "new_search", "gsc_topic_visibility"} <= kinds
    assert facts["inventory"]["total_pages"] >= 3 and any(p["path"] == "/knockdown-rebuild/melbourne" for p in facts["inventory_paths"])
    ids = [f["id"] for f in facts["facts"]]
    assert len(ids) == len(set(ids))


def test_state_scope_only_sees_its_own_state_gaps(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    facts = AN.build_facts(seeded, "SA", WEEK)
    assert all(f.get("state") == "SA" for f in facts["facts"] if f["kind"] == "content_gap_candidate")


def test_a_new_page_for_a_topic_the_site_already_covers_becomes_an_improvement_to_that_page(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    (report, dropped), _ = run(seeded, out(rec(title="Create a knockdown rebuild page", topic_terms=["knockdown rebuild", "melbourne"], state="VIC")))
    r = report["recommendations"][0]
    assert r["type"] == "optimise_page" and r["target_urls"] == ["https://www.metricon.com.au/knockdown-rebuild/melbourne"]
    assert "already has one" in r["adjustment"] and r["title"].startswith("Improve the existing page")


def test_a_genuinely_new_topic_stays_a_create_page_recommendation(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    (report, _), _ = run(seeded, out(rec(title="Create a duplex guide", topic_terms=["bungalow", "conversion"], evidence_ids=["new:1"])))
    r = report["recommendations"][0]
    assert r["type"] == "create_page" and r["existing_pages"] == [] and r["adjustment"] is None


def test_optimise_must_name_pages_that_exist(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    good = rec(type="optimise_page", title="Tighten the knockdown page", target_urls=["/knockdown-rebuild/melbourne"], topic_terms=[], evidence_ids=["new:1"])
    ghost = rec(type="optimise_page", title="Fix a page that is not there", target_urls=["https://www.metricon.com.au/not-a-page"], topic_terms=[], evidence_ids=["new:1"])
    (report, dropped), _ = run(seeded, out(good, ghost))
    assert [r["title"] for r in report["recommendations"]] == ["Tighten the knockdown page"]
    assert any("inventory" in d["reason"] for d in dropped)


def test_unknown_evidence_and_invented_numbers_are_dropped(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    nope = rec(title="Cites nothing real", evidence_ids=["new:999"], topic_terms=["bungalow"])
    invented = rec(title="Invents a figure", why="Searches are up 480% in a week.", topic_terms=["bungalow conversion"], evidence_ids=["new:1"])
    ok = rec(title="Grounded one", why="Granny flat builders is a rising search.", topic_terms=["bungalow conversion"], evidence_ids=["new:1"])
    (report, dropped), _ = run(seeded, out(nope, invented, ok))
    assert [r["title"] for r in report["recommendations"]] == ["Grounded one"]
    reasons = " ".join(d["reason"] for d in dropped)
    assert "no valid evidence" in reasons and "numbers not found" in reasons


def test_duplicate_recommendations_collapse(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    a = rec(title="One", topic_terms=["bungalow"])
    b = rec(title="Two", topic_terms=["bungalow"])
    (report, dropped), _ = run(seeded, out(a, b))
    assert len(report["recommendations"]) == 1 and any("duplicate" in d["reason"] for d in dropped)


def test_competitor_moves_need_competitor_evidence(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    c = AN.CompetitorOut(brand="Carlisle Homes", observation="Did something.", implication="May matter.", evidence_ids=["new:1"])
    (report, _), _ = run(seeded, out(competitor_moves=[c]))
    assert report["competitor_moves"] == []


def test_generate_stores_report_and_current_returns_it_with_cited_evidence(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    n = AN.NewSearchOut(query="granny flat builders", what_people_want="Likely looking for builders of secondary dwellings.", what_to_do="Check the granny flat page.", evidence_ids=["new:1"])
    row = AN.generate(seeded, "AU", WEEK, FakeLLM(out(rec(topic_terms=["bungalow"]), new_in_market=[n])))
    seeded.commit()
    assert row.status == "ok" and row.is_current
    cur = AN.current(seeded, "AU")
    assert cur["report"]["new_in_market"][0]["query"] == "granny flat builders" and "new:1" in cur["report"]["evidence"]
    assert cur["report"]["checked_against_pages"] >= 3


def test_without_a_model_nothing_is_invented_and_computed_tables_remain(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    row = AN.generate(seeded, "AU", WEEK, None)
    seeded.commit()
    assert row.status == "unavailable" and row.report is None
    assert AN.current(seeded, "AU")["report"] is None


def test_a_failed_run_leaves_the_previous_report_visible(seeded, tmp_path):
    setup_data(seeded, tmp_path)
    AN.generate(seeded, "AU", WEEK, FakeLLM(out(rec(topic_terms=["bungalow"]))))
    seeded.commit()

    def boom(user):
        raise LLMError("refused")

    bad = AN.generate(seeded, "AU", WEEK, FakeLLM(boom))
    seeded.commit()
    assert bad.status == "failed" and not bad.is_current
    cur = AN.current(seeded, "AU")
    assert cur["report"] is not None and cur["latest_attempt"]["status"] == "failed"
    assert seeded.scalar(select(InsightReport).where(InsightReport.is_current.is_(True))).id != bad.id


@pytest.fixture
def open_client(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    get_settings.cache_clear()
    from app.main import create_app

    yield TestClient(create_app())
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()


def test_api_serves_computed_opportunities_even_with_no_ai_report(open_client, seeded, tmp_path):
    setup_data(seeded, tmp_path)
    j = open_client.get("/api/analysis?scope=AU").json()
    assert j["report"] is None and j["ai_connected"] is False
    assert j["computed"]["gsc_available"] and j["computed"]["quick_wins"] and j["computed"]["inventory"]["total_pages"] >= 3
    s = open_client.get("/api/analysis?scope=QLD").json()
    assert all(g["state"] == "QLD" for g in s["computed"]["content_gaps"])
