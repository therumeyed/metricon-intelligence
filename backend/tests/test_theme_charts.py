"""Theme charts: sparse keywords are not drawn, keyword selection prefers broad terms and retires sparse ones, momentum comes from the busiest usable keyword. SYNTHETIC series."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import AppSetting, Theme, ThemeKeyword, TrendObservation, TrendPlan, TrendRequest
from app.services import queries as Q
from app.services.trends import metrics as M
from app.services.trends import service as T
from tests.test_trends import pts

TODAY = date(2026, 10, 12)


def test_series_quality_flags_mostly_zero_series_as_sparse_not_as_low_demand():
    good = M.series_quality(pts([30, 40, 35, 50] * 15), TODAY)
    sparse = M.series_quality(pts(([0, 0, 0, 3] * 15)), TODAY)
    short = M.series_quality(pts([30] * 10), TODAY)
    assert good["usable"] and not sparse["usable"] and "rarely" in sparse["reason"] and not short["usable"] and "few" in short["reason"]


def test_direction_needs_real_metrics():
    ok = lambda v: {"status": "ok", "value_pct": v}  # noqa: E731
    no = {"status": "unavailable", "value_pct": None}
    assert M.direction(ok(25), ok(5)) == "rising" and M.direction(ok(-30), no) == "falling" and M.direction(ok(3), ok(2)) == "steady"
    assert M.direction(no, no) == "no_data"
    assert M.direction(ok(40), ok(-30)) == "steady"  # a spike inside a falling year is not called a rise


def mk_request(db, plan, series: dict[str, list[int]]):
    req = TrendRequest(request_hash=f"h{plan.id}", kind="compare", keywords=list(plan.keywords), geo_key=plan.geo_key, geo_level="country", status="ok", purpose="theme", theme_slug=plan.theme_slug)
    db.add(req)
    db.flush()
    for kw, vals in series.items():
        for p in pts(vals):
            db.add(TrendObservation(request_id=req.id, keyword=kw, period_start=p.start, period_end=p.end, value=p.value))
    db.flush()
    T.compute_and_store_metrics(db, req)
    db.flush()
    return req


def test_theme_view_charts_only_usable_keywords_lists_sparse_ones_and_takes_headline_from_the_busiest(seeded, monkeypatch):
    monkeypatch.setattr(Q, "today_local", lambda: TODAY)
    monkeypatch.setattr(T, "today_local", lambda: TODAY)
    plan = TrendPlan(plan_key="theme:display-homes:AU", kind="compare", purpose="theme", geo_key="AU", keywords=["display homes", "display villages", "vastu display"], theme_slug="display-homes", active=True)
    seeded.add(plan)
    seeded.flush()
    mk_request(seeded, plan, {"display homes": [40] * 56 + [60] * 4, "display villages": [4, 5, 6, 5] * 15, "vastu display": [0, 0, 0, 1] * 15})
    seeded.commit()
    t = next(x for x in Q.theme_trends(seeded, "AU") if x["theme"] == "display-homes")
    assert [s["keyword"] for s in t["series"]] == ["display homes", "display villages"]  # busiest first
    assert [s["keyword"] for s in t["sparse"]] == ["vastu display"]
    assert t["headline"]["keyword"] == "display homes" and t["headline"]["direction"] == "rising"
    assert t["series"][1]["quality"]["relative_to_busiest_pct"] < 20  # flagged as swamped on a shared scale


def test_theme_with_no_usable_keyword_is_reported_sparse_not_drawn_as_flat_lines(seeded, monkeypatch):
    monkeypatch.setattr(Q, "today_local", lambda: TODAY)
    monkeypatch.setattr(T, "today_local", lambda: TODAY)
    plan = TrendPlan(plan_key="theme:acreage:AU", kind="compare", purpose="theme", geo_key="AU", keywords=["acreage homes", "acreage designs"], theme_slug="acreage", active=True)
    seeded.add(plan)
    seeded.flush()
    mk_request(seeded, plan, {"acreage homes": [0, 0, 1, 0] * 15, "acreage designs": [0, 0, 0, 2] * 15})
    seeded.commit()
    t = next(x for x in Q.theme_trends(seeded, "AU") if x["theme"] == "acreage")
    assert t["status"] == "sparse" and t["series"] == [] and len(t["sparse"]) == 2 and t["headline"] is None


def test_default_theme_plans_start_from_broad_seed_terms_not_niche_gsc_terms(seeded):
    th = seeded.scalar(select(Theme).where(Theme.slug == "styles-features"))
    seeded.add(ThemeKeyword(theme_id=th.id, keyword="lumiere facade", source="gsc", tracked=True, gsc_impressions=99999))
    seeded.flush()
    kws = T.theme_keywords(seeded, th)
    assert kws[:4] == ["modern farmhouse", "hamptons style homes", "house facades", "vastu home design"] and "lumiere facade" in kws and len(kws) == 5


def test_sparse_keywords_are_retired_and_replaced_at_the_next_selection(seeded, monkeypatch):
    monkeypatch.setattr(T, "today_local", lambda: TODAY)
    th = seeded.scalar(select(Theme).where(Theme.slug == "styles-features"))
    seeded.add(ThemeKeyword(theme_id=th.id, keyword="lumiere facade", source="gsc", tracked=True, gsc_impressions=500))
    seeded.flush()
    plan = TrendPlan(plan_key="theme:styles-features:AU", kind="compare", purpose="theme", geo_key="AU", keywords=T.theme_keywords(seeded, th), theme_slug=th.slug, active=True)
    seeded.add(plan)
    seeded.flush()
    mk_request(seeded, plan, {"modern farmhouse": [30, 35, 40, 38] * 15, "hamptons style homes": [20, 25, 22, 24] * 15, "house facades": [50, 55, 52, 51] * 15,
                              "vastu home design": [0, 0, 0, 1] * 15, "lumiere facade": [20, 21, 22, 23] * 15})
    seeded.commit()
    assert T.mark_unusable_keywords(seeded) == ["vastu home design"]
    assert "vastu home design" in seeded.get(AppSetting, T.UNUSABLE_KEY).value
    assert T.reselect_theme_plans(seeded) == 1
    assert "vastu home design" not in plan.keywords and len(plan.keywords) == 4  # only four usable candidates exist
    assert T.mark_unusable_keywords(seeded) == []  # idempotent


def test_admin_edited_plans_are_never_re_keyed(seeded):
    th = seeded.scalar(select(Theme).where(Theme.slug == "display-homes"))
    plan = TrendPlan(plan_key="theme:display-homes:AU", kind="compare", purpose="theme", geo_key="AU", keywords=["a one", "b two"], theme_slug=th.slug, auto_managed=False)
    seeded.add(plan)
    seeded.flush()
    assert T.reselect_theme_plans(seeded) == 0 and plan.keywords == ["a one", "b two"]
