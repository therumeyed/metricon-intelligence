"""Trends: provider contract (mocked HTTP with the documented response shape - TEST DATA ONLY), caching, partial data, zero baselines."""
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import TrendObservation, TrendPlan, TrendRequest
from app.services import queries as Q, spend
from app.services.trends import metrics as M
from app.services.trends import service as T
from app.services.trends.provider import DataForSEOTrends, ProviderError

TODAY = date(2026, 10, 12)


def weeks(n, end_monday=date(2026, 10, 5)):
    return [end_monday - timedelta(days=7 * i) for i in range(n - 1, -1, -1)]


def pts(values, partial_last=False, end_monday=date(2026, 10, 5)):
    ws = weeks(len(values), end_monday)
    return [M.Point(w, w + timedelta(days=6), v, partial_last and i == len(values) - 1) for i, (w, v) in enumerate(zip(ws, values))]


# --------------------------------------------------------------------------- pure maths
def test_four_week_change_uses_latest_complete_weeks_and_excludes_partial():
    series = pts([10] * 4 + [20] * 4 + [999], partial_last=True)  # the last (partial) week is huge but must be ignored
    ch = M.four_week_change(series, TODAY)
    assert ch.status == "ok" and ch.value_pct == 100.0 and ch.current_avg == 20 and ch.comparison_avg == 10


def test_in_progress_week_is_excluded_even_without_provider_flag():
    series = pts([10] * 4 + [20] * 4 + [500], end_monday=date(2026, 10, 12))  # week of 12-18 Oct has not finished on 12 Oct
    ch = M.four_week_change(series, TODAY)
    assert ch.status == "ok" and ch.current_avg == 20


@pytest.mark.parametrize("vals,reason", [([0, 0, 0, 0, 3, 2, 3, 2], "baseline_too_low"), ([0] * 8, "baseline_too_low"), ([5, 5, 5], "insufficient_observations")])
def test_zero_or_missing_baseline_gives_unavailable_not_a_misleading_percentage(vals, reason):
    ch = M.four_week_change(pts(vals), TODAY)
    assert ch.status == "unavailable" and ch.reason == reason and ch.value_pct is None


def test_missing_values_are_never_treated_as_zero():
    series = pts([10, 10, 10, 10, None, 20, 20, 20, 20])
    ch = M.four_week_change(series, TODAY)
    assert ch.status == "unavailable" and ch.reason in ("gaps_in_series", "insufficient_observations")  # the hole is a hole, not a 0 baseline


def test_year_on_year_needs_matching_history():
    assert M.year_on_year(pts([10] * 20), TODAY).reason == "no_year_ago_history"
    long = pts([10] * 60 + [20] * 4)
    ch = M.year_on_year(long, TODAY)
    assert ch.status == "ok" and ch.value_pct == 100.0


def test_sparkline_is_individually_normalised_and_keeps_gaps():
    sp = M.normalise_for_sparkline(pts([5, None, 10]))
    assert [p["v"] for p in sp] == [50.0, None, 100.0]


# --------------------------------------------------------------------------- provider + service with a mocked transport
class Provider:
    """Mock of the documented DataForSEO response shapes. Counts calls so caching can be asserted."""

    def __init__(self, fail=False, with_sa=False):
        self.posts, self.fail, self.with_sa = [], fail, with_sa

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/locations/au"):
            res = [{"location_code": 7000001, "location_name": "Australia", "location_type": "Country", "country_iso_code": "AU", "location_code_parent": None},
                   {"location_code": 7000002, "location_name": "Victoria", "location_type": "State", "country_iso_code": "AU", "location_code_parent": 7000001},
                   {"location_code": 7000003, "location_name": "New South Wales", "location_type": "State", "country_iso_code": "AU", "location_code_parent": 7000001},
                   {"location_code": 7000004, "location_name": "Queensland", "location_type": "State", "country_iso_code": "AU", "location_code_parent": 7000001}]
            if self.with_sa:
                res.append({"location_code": 7000005, "location_name": "South Australia", "location_type": "State", "country_iso_code": "AU", "location_code_parent": 7000001})
            return httpx.Response(200, json={"status_code": 20000, "tasks": [{"status_code": 20000, "result": res}]})
        import json

        body = json.loads(request.content)[0]
        self.posts.append(body)
        if self.fail:
            return httpx.Response(500, text="boom")
        kws = body["keywords"]
        data = []
        ws = weeks(70)
        for i, w in enumerate(ws):
            vals = [(20 + (j * 7) + (i % 5)) if i < 66 else (40 + j) for j, _ in enumerate(kws)]
            data.append({"date_from": w.isoformat(), "date_to": (w + timedelta(days=6)).isoformat(), "timestamp": 0, "missing_data": i == len(ws) - 1, "values": vals})
        item = {"type": "google_trends_graph", "keywords": kws, "data": data}
        return httpx.Response(200, json={"status_code": 20000, "cost": 0.0123, "tasks": [{"status_code": 20000, "cost": 0.0123, "result": [{"keywords": kws, "location_code": body.get("location_code"), "items": [item]}]}]})


@pytest.fixture
def prov(seeded, monkeypatch):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "test")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "test")
    get_settings.cache_clear()
    p = Provider()
    client = DataForSEOTrends(httpx.Client(transport=httpx.MockTransport(p.handler), auth=("t", "t")))
    T.sync_locations(seeded, client)
    seeded.commit()
    yield p, client
    get_settings.cache_clear()


def test_locations_resolved_from_catalogue_not_hardcoded(seeded, prov):
    assert T.resolve_location(seeded, "AU").location_code == 7000001
    assert T.resolve_location(seeded, "VIC").location_code == 7000002
    assert T.resolve_location(seeded, "SA") is None  # absent from the catalogue => unsupported, not guessed
    assert T.resolve_location(seeded, "city:Melbourne") is None


def test_capability_test_marks_unsupported_geography_explicitly(seeded, prov):
    p, client = prov
    res = T.capability_test(seeded, client, ["AU", "VIC", "SA", "city:Melbourne"])
    by = {r["geo"]: r for r in res}
    assert by["AU"]["supported"] and by["VIC"]["supported"]
    assert by["SA"]["supported"] is False and "catalogue" in by["SA"]["detail"]["reason"]
    assert by["city:Melbourne"]["supported"] is False
    assert {c["geo"]: c["supported"] for c in Q.trend_capabilities(seeded)}["SA"] is False


def test_comparison_is_cached_by_all_parameters_and_stores_complete_evidence(seeded, prov):
    p, client = prov
    kws = ["metricon", "arden homes", "carlisle homes"]
    r1 = T.fetch_compare(seeded, client, kws, "VIC")
    seeded.commit()
    r2 = T.fetch_compare(seeded, client, kws[::-1], "VIC")  # same request, keyword order irrelevant
    assert r1.id == r2.id and len(p.posts) == 1
    T.fetch_compare(seeded, client, kws, "AU")  # different geography => a different request
    T.fetch_compare(seeded, client, kws, "VIC", category=13)  # different category too
    assert len(p.posts) == 3
    assert r1.raw_response and r1.geo_level == "state" and r1.location_name == "Victoria" and r1.search_type == "web"
    assert float(r1.cost_reported) == pytest.approx(0.0123) and r1.cost_estimated is not None  # reported and estimated tracked separately
    assert p.posts[0]["location_code"] == 7000002 and len(p.posts[0]["keywords"]) == 3


def test_partial_period_is_flagged_and_excluded_from_metrics(seeded, prov):
    p, client = prov
    r = T.fetch_compare(seeded, client, ["metricon", "arden homes"], "AU")
    obs = list(seeded.scalars(select(TrendObservation).where(TrendObservation.request_id == r.id, TrendObservation.keyword == "metricon").order_by(TrendObservation.period_start)))
    assert obs[-1].is_partial and not obs[-2].is_partial
    from app.models import TrendMetric

    m = seeded.scalar(select(TrendMetric).where(TrendMetric.request_id == r.id, TrendMetric.keyword == "metricon", TrendMetric.metric_key == "mom4"))
    assert m.status == "ok" and m.details["current_window"][1] < "2026-10-12"  # window ends before the partial final week


def test_more_than_five_keywords_rejected(seeded, prov):
    p, client = prov
    with pytest.raises(ProviderError):
        T.fetch_compare(seeded, client, list("abcdef"), "AU")
    assert not p.posts


def test_failed_provider_call_stores_no_observations_and_no_zeros(seeded, monkeypatch):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "t")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "t")
    get_settings.cache_clear()
    good = Provider()
    c1 = DataForSEOTrends(httpx.Client(transport=httpx.MockTransport(good.handler), auth=("t", "t")))
    T.sync_locations(seeded, c1)
    seeded.commit()
    bad = DataForSEOTrends(httpx.Client(transport=httpx.MockTransport(Provider(fail=True).handler), auth=("t", "t")))
    with pytest.raises(ProviderError):
        T.fetch_compare(seeded, bad, ["metricon", "arden homes"], "AU")
    seeded.commit()
    req = seeded.scalars(select(TrendRequest)).one()
    assert req.status == "failed" and seeded.scalar(select(func.count()).select_from(TrendObservation)) == 0
    get_settings.cache_clear()


def test_spend_limits_stop_requests_before_any_call(seeded, prov):
    p, client = prov
    from app.models import AppSetting

    seeded.add(AppSetting(key="dataforseo_daily_usd_limit", value=0.001))
    seeded.commit()
    with pytest.raises(spend.BudgetExceeded):
        T.fetch_compare(seeded, client, ["metricon", "arden homes"], "AU")
    assert not p.posts
    seeded.rollback()
    seeded.merge(AppSetting(key="dataforseo_daily_usd_limit", value=100))
    seeded.merge(AppSetting(key="dataforseo_max_requests_per_job", value=1))
    seeded.commit()
    with pytest.raises(spend.BudgetExceeded):
        T.fetch_compare(seeded, client, ["metricon", "arden homes"], "VIC", requests_so_far=1)


def test_default_plans_group_brands_in_fives_with_metricon_as_anchor_and_no_cross_group_ranking(seeded, prov):
    T.build_default_plans(seeded)
    seeded.commit()
    vic = list(seeded.scalars(select(TrendPlan).where(TrendPlan.geo_key == "VIC", TrendPlan.purpose == "brands").order_by(TrendPlan.id)))
    assert len(vic) == 2 and all(len(p.keywords) <= 5 and p.keywords[0] == "metricon" for p in vic)
    assert sum(len(p.keywords) - 1 for p in vic) == 6  # all six VIC competitors covered across comparable groups
    assert seeded.scalar(select(TrendPlan).where(TrendPlan.plan_key == "brands:AU:1")) is not None  # national plan is its own request
    assert not any(p.active for p in seeded.scalars(select(TrendPlan).where(TrendPlan.plan_key == "brands:SA:1")))  # only the pilot starts active
    view = Q.brand_trend_groups(seeded, "VIC")
    assert len(view["groups"]) == 2 and all("rank" not in str(g).lower() for g in view["groups"]) and "not search volumes" in view["note"]


def test_brand_groups_report_actual_geography_and_scale(seeded, prov):
    p, client = prov
    T.build_default_plans(seeded)
    T.refresh_plans(seeded, client, only_pilot=True)
    seeded.commit()
    au = Q.brand_trend_groups(seeded, "AU")
    g = au["groups"][0]
    assert g["status"] == "ok" and g["meta"]["geo"] == "AU" and g["meta"]["location_name"] == "Australia" and "Do not compare across groups" in g["scale_note"]
    assert au["geo_label"].startswith("Australia (national")
    vic = Q.brand_trend_groups(seeded, "VIC")
    assert vic["geo_label"] == "Victoria (statewide)"  # a statewide series is never relabelled as a city
    s = g["series"][0]
    assert s["sparkline_individually_normalised"] and s["mom4"]["status"] == "ok"
