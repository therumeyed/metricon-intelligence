"""What happens when keys are missing, wrong, or arrive later. Synthetic fixtures only."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.models import Brief, CompetitorEvent, JobRun, Offer
from app.services import events, jobs, llm as llm_mod
from app.services.offers.schema import PageExtraction
from tests.helpers import FakeLLM, page
from tests.test_offers import MAIN, cycle, eo, only_offer, src

H = {"X-Requested-With": "metricon-ui"}


def test_pasted_keys_with_whitespace_still_count_as_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-abc\n")
    monkeypatch.setenv("CLAUDE_MODEL", " claude-sonnet-5-5 ")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "me@example.test ")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "pw\n")
    s = Settings()
    assert s.anthropic_ready and s.dataforseo_ready
    assert s.anthropic_api_key == "sk-abc" and s.claude_model == "claude-sonnet-5-5"


def test_missing_model_is_reported_separately_from_missing_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-abc")
    monkeypatch.setenv("CLAUDE_MODEL", "")
    get_settings.cache_clear()
    assert llm_mod.status() == {"connected": False, "reason": "CLAUDE_MODEL not set"}
    get_settings.cache_clear()


def test_weekly_brief_job_without_ai_writes_nothing_instead_of_spamming_rows(seeded):
    import app.jobs  # noqa: F401

    for _ in range(3):  # the hourly dispatcher re-tries a skipped weekly job every hour
        out = jobs.run_now("generate_briefs", trigger="schedule")
        assert out["status"] == "skipped" and "not_connected" in out
    assert seeded.scalar(select(func.count()).select_from(Brief)) == 0


def test_every_job_records_what_its_own_process_could_see(seeded):
    import app.jobs  # noqa: F401

    jobs.run_now("generate_briefs", trigger="schedule")
    run = seeded.scalars(select(JobRun).where(JobRun.job_name == "generate_briefs")).first()
    assert run.result["connections"] == {"ai": False, "trends": False}  # lets Admin show web-vs-cron differences


def test_connection_test_needs_an_admin_session_when_login_is_required(seeded):
    from app.main import create_app

    assert TestClient(create_app()).post("/api/admin/test-connections", headers=H).status_code == 401


def test_connection_test_in_open_mode(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "")
    get_settings.cache_clear()
    from app.main import create_app

    r = TestClient(create_app()).post("/api/admin/test-connections", headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["anthropic"]["ok"] is False and "ANTHROPIC_API_KEY" in j["anthropic"]["detail"]
    assert j["dataforseo"]["ok"] is False and "DATAFORSEO_LOGIN" in j["dataforseo"]["detail"]
    assert j["environment_seen_by_web_service"] == {"ANTHROPIC_API_KEY": False, "CLAUDE_MODEL": False, "DATAFORSEO_LOGIN": False, "DATAFORSEO_PASSWORD": False}
    assert "cron" in j["note"]
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()


def test_rules_offers_are_re_read_by_the_model_once_it_connects_without_fake_news(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page("<h2>Up to $82K in luxury inclusions</h2><p>Offer ends 30 November 2026.</p>" + MAIN), llm=None)
    before = seeded.scalar(select(func.count()).select_from(Offer))
    assert before >= 1 and all(o.versions[-1].extraction_method == "rules" for o in seeded.scalars(select(Offer)))
    llm = FakeLLM(PageExtraction(offers=[eo()]))
    # same page, same text - previously this was skipped as 'unchanged' and the weak rules extraction stayed forever
    cycle(seeded, monkeypatch, s, page("<h2>Up to $82K in luxury inclusions</h2><p>Offer ends 30 November 2026.</p>" + MAIN), llm=llm)
    assert llm.calls == 1
    ai = [o for o in seeded.scalars(select(Offer)) if o.versions[-1].extraction_method == "llm"]
    assert ai and all(o.is_baseline for o in ai)  # the upgrade is a re-baseline, not a launch
    assert events.events_from_offers(seeded, __import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc)) == 0
    assert seeded.scalar(select(func.count()).select_from(CompetitorEvent)) == 0
    old = [o for o in seeded.scalars(select(Offer)) if o.versions[-1].extraction_method == "rules"]
    assert old and all(o.status == "no_longer_observed" and "fuller AI re-read" in o.status_reason for o in old)  # superseded, not 'withdrawn'
    # and from then on an unchanged page is skipped again
    cycle(seeded, monkeypatch, s, page("<h2>Up to $82K in luxury inclusions</h2><p>Offer ends 30 November 2026.</p>" + MAIN), llm=llm)
    assert llm.calls == 1


def test_activate_all_trend_plans(seeded, monkeypatch):
    from app.models import TrendPlan
    from app.services.trends import service as T

    T.build_default_plans(seeded)
    seeded.commit()
    inactive = seeded.scalar(select(func.count()).select_from(TrendPlan).where(TrendPlan.active.is_(False)))
    assert inactive > 0
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    get_settings.cache_clear()
    from app.main import create_app

    r = TestClient(create_app()).post("/api/admin/trend-plans/activate-all", headers=H)
    assert r.json()["activated"] == inactive
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
