"""Reader API (viewer and admin). Every route sits behind the session dependency added in main.py."""
from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Brand, JobRun, MacroSeries, NewsItem, Offer, RelatedQueryCandidate, Theme
from ..services import briefs as B
from ..services import llm as llm_mod
from ..services import macro
from ..services import queries as Q
from ..timeutil import now_utc, today_local

router = APIRouter(prefix="/api", tags=["reader"])
GEOS = ["AU", "VIC", "NSW", "QLD", "SA"]


def _csv(rows: list[dict], cols: list[str], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _geo(geo: str) -> str:
    geo = geo.upper()
    if geo not in GEOS:
        raise HTTPException(400, f"geo must be one of {GEOS}")
    return geo


# --------------------------------------------------------------------------- meta
@router.get("/meta")
def meta(db: Session = Depends(get_db)):
    s = get_settings()
    brands = []
    for b in db.scalars(select(Brand).where(Brand.active.is_(True)).order_by(Brand.is_own.desc(), Brand.name)):
        brands.append({"slug": b.slug, "name": b.name, "is_own": b.is_own, "states": sorted(r.state for r in b.regions if r.active)})
    last_jobs = {}
    for name, status, fin in db.execute(select(JobRun.job_name, JobRun.status, JobRun.finished_at).where(JobRun.finished_at.is_not(None)).order_by(JobRun.finished_at.desc())):
        last_jobs.setdefault(name, {"status": status, "finished_at": fin})
    from ..models import TrendRequest

    trend_ok = db.scalar(select(func.count()).select_from(TrendRequest).where(TrendRequest.status == "ok", TrendRequest.kind == "compare")) or 0
    return {
        "states": [{"code": "AU", "name": "Australia", "city": None}, {"code": "VIC", "name": "Victoria", "city": "Melbourne"}, {"code": "NSW", "name": "New South Wales", "city": "Sydney"},
                   {"code": "QLD", "name": "Queensland", "city": "Brisbane"}, {"code": "SA", "name": "South Australia", "city": "Adelaide"}],
        "brands": brands, "themes": [{"slug": t.slug, "name": t.name} for t in db.scalars(select(Theme).where(Theme.active.is_(True)).order_by(Theme.sort_order))],
        "modules": {
            "ai": llm_mod.status(),
            "trends": {"connected": s.dataforseo_ready, "reason": None if s.dataforseo_ready else "DataForSEO credentials not set", "stored_requests": trend_ok},
            "offers": {"connected": True, "offers": db.scalar(select(func.count()).select_from(Offer))},
            "macro": {"connected": True}, "news": {"connected": True},
        },
        "last_jobs": last_jobs, "today": today_local(), "timezone": s.app_timezone,
        "roster_note": "Coverage is the monitored roster of 16 brands (22 brand/state combinations), not every Australian builder.",
    }


# --------------------------------------------------------------------------- AI analysis + search opportunities
_OPP_CACHE: dict = {}


def _opportunities(db: Session, scope: str) -> dict:
    import time

    from ..services import analysis as AN
    from ..services import opportunities as O

    hit = _OPP_CACHE.get(scope)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    opp = O.compute(db)
    st = None if scope == "AU" else scope
    pick = lambda items, key="state_hint": items if st is None else [i for i in items if i.get(key) in (st, None)]  # noqa: E731
    val = {"gsc_available": opp["gsc_available"], "period": opp.get("period"), "gaps": opp["gaps"], "inventory": {k: v for k, v in opp["inventory"].items() if k != "paths"},
           "theme_scorecard": opp["theme_scorecard"], "quick_wins": pick(opp["quick_wins"]), "low_ctr": pick(opp["low_ctr"]),
           "content_gaps": [g for g in opp["content_gaps"] if st is None or g["state"] == st], "topic_visibility": pick(opp["topic_visibility"]), "new_searches": opp["new_searches"]}
    _OPP_CACHE[scope] = (time.time(), val)
    return val


@router.get("/analysis")
def analysis(scope: str = "AU", db: Session = Depends(get_db)):
    from ..services import analysis as AN
    from ..services import llm as llm_mod

    sc = _geo(scope)
    running = db.scalar(select(JobRun).where(JobRun.job_name == "generate_analysis", JobRun.status.in_(["queued", "running"])).order_by(JobRun.id.desc()).limit(1))
    return {**AN.current(db, sc), "computed": _opportunities(db, sc), "ai_connected": llm_mod.available(), "generating": running is not None,
            "note": "Search Console shows Metricon's own visibility only. Google Trends is relative interest, not volume."}


# --------------------------------------------------------------------------- brief
@router.get("/brief")
def brief(scope: str = "AU", db: Session = Depends(get_db)):
    return B.current_brief(db, _geo(scope))


@router.get("/brief/export.md")
def brief_md(scope: str = "AU", db: Session = Depends(get_db)):
    cur = B.current_brief(db, _geo(scope))
    if not cur["brief"]:
        raise HTTPException(404, "Brief unavailable")
    return PlainTextResponse(B.brief_markdown(cur["brief"]), headers={"Content-Disposition": f'attachment; filename="market-brief-{scope.lower()}-{cur["brief"]["week_start"]}.md"'},
                             media_type="text/markdown")


# --------------------------------------------------------------------------- demand
@router.get("/demand/brands")
def demand_brands(geo: str = "AU", db: Session = Depends(get_db)):
    return Q.brand_trend_groups(db, _geo(geo))


@router.get("/demand/themes")
def demand_themes(geo: str = "AU", db: Session = Depends(get_db)):
    return {"geo": _geo(geo), "geo_label": Q.geo_view_label(geo.upper()), "themes": Q.theme_trends(db, geo.upper())}


@router.get("/demand/capabilities")
def demand_caps(db: Session = Depends(get_db)):
    return Q.trend_capabilities(db)


@router.get("/demand/gsc")
def demand_gsc(db: Session = Depends(get_db)):
    return Q.gsc_overview(db)


@router.get("/demand/related")
def demand_related(db: Session = Depends(get_db)):
    rows = db.scalars(select(RelatedQueryCandidate).where(RelatedQueryCandidate.relevant.is_(True)).order_by(RelatedQueryCandidate.first_seen_at.desc()).limit(60))
    return [{"id": r.id, "seed": r.seed, "query": r.query, "kind": r.kind, "value": r.value, "geo": r.geo_key, "theme": r.suggested_theme_slug, "review_status": r.review_status,
             "validated_trend": r.validated_trend, "label": "Proposed subtheme - not an established trend until validated" if not r.validated_trend else "Validated over time"} for r in rows]


@router.get("/demand/export.csv")
def demand_export(geo: str = "AU", kind: str = "brands", db: Session = Depends(get_db)):
    geo = _geo(geo)
    rows = []
    if kind == "brands":
        for g in Q.brand_trend_groups(db, geo)["groups"]:
            if g["status"] != "ok":
                continue
            for s in g["series"]:
                for p in s["points"]:
                    rows.append({"geography": geo, "geography_level": g["meta"]["geo_level"], "group": g["plan_key"], "brand": s.get("brand"), "keyword": s["keyword"],
                                 "period_start": p["date"], "period_end": p["end"], "value_index_0_100_group_scale": p["value"], "partial_period": p["partial"],
                                 "retrieved_at": g["meta"]["retrieved_at"], "provider": g["meta"]["provider"], "mom4_pct": s["mom4"].get("value_pct"), "yoy4_pct": s["yoy4"].get("value_pct")})
    else:
        for t in Q.theme_trends(db, geo):
            if t["status"] != "ok":
                continue
            for s in t["series"]:
                for p in s["points"]:
                    rows.append({"geography": geo, "geography_level": t["meta"]["geo_level"], "theme": t["theme_name"], "keyword": s["keyword"], "period_start": p["date"],
                                 "period_end": p["end"], "value_index_0_100_group_scale": p["value"], "partial_period": p["partial"], "retrieved_at": t["meta"]["retrieved_at"],
                                 "provider": t["meta"]["provider"], "mom4_pct": s["mom4"].get("value_pct"), "yoy4_pct": s["yoy4"].get("value_pct")})
    cols = ["geography", "geography_level", "group", "theme", "brand", "keyword", "period_start", "period_end", "value_index_0_100_group_scale", "partial_period", "mom4_pct", "yoy4_pct", "retrieved_at", "provider"]
    return _csv(rows, cols, f"demand-{kind}-{geo.lower()}-{today_local()}.csv")


# --------------------------------------------------------------------------- offers
def _offer_filters(state, brands, benefit, status, range_, buyer, current_only, as_of):
    return dict(state=state.upper() if state and state.upper() != "AU" else None, brands=brands, benefit_types=benefit, statuses=status, range_q=range_, buyer_q=buyer,
                current_only=current_only, as_of=as_of)


@router.get("/offers")
def offers(state: str | None = None, brand: list[str] = Query(default=[]), benefit: list[str] = Query(default=[]), status: list[str] = Query(default=[]),
           range: str | None = None, buyer: str | None = None, current_only: bool = True, as_of: date | None = None, db: Session = Depends(get_db)):
    flt = _offer_filters(state, brand or None, benefit or None, status or None, range, buyer, current_only and not as_of, as_of)
    rows = [Q.offer_dict(db, o) for o in Q.list_offers(db, **flt)]
    own = [r for r in rows if r["is_own"]]
    return {"view": "historical" if as_of else "current", "as_of": as_of, "retrieved": now_utc(), "geography": flt["state"] or "AU", "count": len(rows), "offers": rows,
            "metricon_count": len(own), "roster_note": "Monitored roster only. Offer geography is shown as stated on the page; unknown geography is not assumed to be national.",
            "own_blocked": [s for s in Q.source_health(db) if s["brand"] == "Metricon" and s["kind"] == "offer_listing" and s["last_outcome"] not in (None, "ok")][:8],
            "search": Q.search_status(db)}


@router.get("/offers/patterns")
def offers_patterns(state: str | None = None, days: int = 30, db: Session = Depends(get_db)):
    return Q.promo_patterns(db, state.upper() if state and state.upper() != "AU" else None, days)


@router.get("/offers/export.csv")
def offers_export(state: str | None = None, brand: list[str] = Query(default=[]), benefit: list[str] = Query(default=[]), status: list[str] = Query(default=[]),
                  range: str | None = None, buyer: str | None = None, current_only: bool = True, as_of: date | None = None, db: Session = Depends(get_db)):
    flt = _offer_filters(state, brand or None, benefit or None, status or None, range, buyer, current_only and not as_of, as_of)
    rows = []
    for o in Q.list_offers(db, **flt):
        d = Q.offer_dict(db, o)
        for m in (d["mechanisms"] or [{}]):
            rows.append({"campaign_id": d["campaign_id"], "brand": d["brand"], "offer": d["title"], "status": d["status_label"], "freshness": d["freshness"],
                         "geography": d["geography_label"], "benefit_type": m.get("type_label"), "benefit": m.get("label"), "amount": m.get("amount"), "amount_qualifier": m.get("qualifier"),
                         "amount_kind": m.get("kind"), "customer_payment_required": m.get("customer_payment_required"), "government_component": m.get("government_component"),
                         "time_promise_type": m.get("time_promise_type"), "time_promise_days": m.get("time_promise_days"), "ranges": "; ".join(d["ranges"]),
                         "buyer_types": "; ".join(d["buyer_types"]), "finance_requirements": "; ".join(d["finance_requirements"]), "exclusions": "; ".join(d["exclusions"]),
                         "contract_condition": d["contract_condition"], "published_start": d["published_start"], "published_expiry": d["published_expiry"],
                         "expiry_note": d["expiry_label"], "first_observed_at": d["first_observed_at"], "baseline_capture": d["is_baseline"], "last_success_check_at": d["last_success_check_at"],
                         "needs_review": d["needs_review"], "source_url": d["source_url"], "terms_url": d["terms_url"], "as_of": as_of or today_local(),
                         "view": "historical" if as_of else "current"})
    cols = ["campaign_id", "brand", "offer", "status", "freshness", "geography", "benefit_type", "benefit", "amount", "amount_qualifier", "amount_kind", "customer_payment_required",
            "government_component", "time_promise_type", "time_promise_days", "ranges", "buyer_types", "finance_requirements", "exclusions", "contract_condition", "published_start",
            "published_expiry", "expiry_note", "first_observed_at", "baseline_capture", "last_success_check_at", "needs_review", "source_url", "terms_url", "view", "as_of"]
    return _csv(rows, cols, f"offers-{(state or 'au').lower()}-{today_local()}.csv")


@router.get("/offers/{campaign_id}")
def offer_detail(campaign_id: str, db: Session = Depends(get_db)):
    o = db.scalar(select(Offer).where(Offer.campaign_id == campaign_id))
    if not o:
        raise HTTPException(404, "Offer not found")
    d = Q.offer_dict(db, o, detail=True)
    from ..models import OfferCorrection

    d["corrections"] = [{"field": c.field, "value": c.value, "machine_value": c.machine_value, "reason": c.reason, "at": c.created_at, "superseded": c.superseded_at is not None}
                        for c in db.scalars(select(OfferCorrection).where(OfferCorrection.offer_id == o.id).order_by(OfferCorrection.id.desc()))]
    return d


# --------------------------------------------------------------------------- competitor activity
@router.get("/events")
def events(state: str | None = None, brand: list[str] = Query(default=[]), event_type: str | None = None, days: int = 60, db: Session = Depends(get_db)):
    st = state.upper() if state and state.upper() != "AU" else None
    ev = Q.events_list(db, state=st, brands=brand or None, event_type=event_type, days=days)
    from ..models import TrackedItem

    tracked = db.scalar(select(func.count()).select_from(TrackedItem)) or 0
    return {"events": ev, "count": len(ev), "tracked_items": tracked, "window_days": days,
            "note": "Dates are when our crawler first saw a change. They are not confirmed launch dates unless an effective date is shown."}


# --------------------------------------------------------------------------- macro
@router.get("/macro")
def macro_view(geo: str = "AU", db: Session = Depends(get_db)):
    geo = _geo(geo)
    series = list(db.scalars(select(MacroSeries).where(MacroSeries.active.is_(True), MacroSeries.geography.in_(["AU", geo])).order_by(MacroSeries.indicator, MacroSeries.id)))
    out = []
    for s in series:
        if geo != "AU" and s.indicator != "cash_rate" and s.geography != geo:
            continue
        out.append(macro.series_view(db, s, 60))
    # state view: also include the national series for context, clearly labelled
    return {"geo": geo, "series": out, "missing_state_coverage": [] if geo != "AU" else [],
            "notes": ["The cash rate target is one national rate shared by every state.",
                      "Approvals are private-sector houses (original data); the PPI is a house-construction OUTPUT index. Neither is an input-cost index."]}


@router.get("/macro/conditions")
def macro_conditions(geo: str = "AU", db: Session = Depends(get_db)):
    from ..services import macro as MC

    return MC.conditions(db, _geo(geo))


@router.get("/macro/compare")
def macro_compare(db: Session = Depends(get_db)):
    from ..services import macro as MC

    return MC.compare_states(db)


@router.get("/macro/calendar")
def macro_calendar(db: Session = Depends(get_db)):
    from ..services import macro as MC

    return MC.release_calendar(db)


TOPICS = [
    ("rates", "Rates & lending", r"\b(rba|reserve bank|cash rate|interest rate|rate (cut|hike|rise)|mortgage|home loan|lending|lender|borrow|repayment|apra)\b"),
    ("policy", "Policy & grants", r"\b(grant|stamp duty|first home|scheme|government|minister|policy|budget|planning|zoning|reform|legislation|tax|incentive|federal|state government)\b"),
    ("costs", "Costs & supply", r"\b(cost|price[sd]?|inflation|materials|labour|shortage|supply chain|insolven|liquidat|collapse|timber|steel|trades|wages)\b"),
    ("activity", "Building activity & demand", r"\b(approvals?|commencements?|construction|housing (starts|supply|demand|market)|building (activity|industry)|new homes|dwelling|house prices?|auction|sales volumes?)\b"),
]


def topic_of(title: str) -> tuple[str, str]:
    import re as _re

    for key, label, rx in TOPICS:
        if _re.search(rx, title, _re.I):
            return key, label
    return "other", "Other housing news"


@router.get("/news")
def news(state: str | None = None, limit: int = 30, topic: str | None = None, db: Session = Depends(get_db)):
    q = select(NewsItem).where(NewsItem.relevance == "kept").order_by(NewsItem.published_at.desc().nullslast()).limit(min(limit, 100))
    if state and state.upper() != "AU":
        q = q.where((NewsItem.state == state.upper()) | NewsItem.state.is_(None))
    rows = list(db.scalars(q))
    if topic:
        rows = [n for n in rows if topic_of(n.title)[0] == topic]
    return [{"id": n.id, "topic": topic_of(n.title)[0], "topic_label": topic_of(n.title)[1], "title": n.title, "publisher": n.publisher, "published_at": n.published_at, "retrieved_at": n.retrieved_at, "event_date": n.event_date,
             "url": n.resolved_url or n.canonical_url, "source_class": n.source_class, "evidence_level": n.evidence_level, "state": n.state, "also_reported_by": n.also_reported_by,
             "label": "Headline only" if n.evidence_level == "headline_only" else "Article"} for n in rows]
