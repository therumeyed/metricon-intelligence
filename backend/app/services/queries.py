"""Read-side helpers shared by the API, exports and the brief generator. Facts are computed here, never by the model."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import (
    Brand,
    BrandRegion,
    CompetitorEvent,
    GscImport,
    Offer,
    OfferMechanism,
    Source,
    Theme,
    TrendCapability,
    TrendMetric,
    TrendObservation,
    TrendPlan,
    TrendRequest,
)
from ..timeutil import now_utc
from .offers import pipeline
from .offers.schema import MECHANISM_LABELS
from .trends import metrics as M
from .trends.provider import clean_keyword

STATES = ["VIC", "NSW", "QLD", "SA"]
STATUS_LABELS = {"advertised": "Advertised", "expired": "Expired", "withdrawn": "Withdrawn (stated)", "no_longer_observed": "No longer observed", "needs_review": "Needs review"}


def own_brand(db: Session) -> Brand | None:
    return db.scalar(select(Brand).where(Brand.is_own.is_(True)))


# --------------------------------------------------------------------------- offers
def _money(v) -> float | None:
    return float(v) if v is not None else None


def offer_dict(db: Session, o: Offer, detail: bool = False, now: datetime | None = None) -> dict:
    now = now or now_utc()
    eff = o.effective or {}
    fr = pipeline.freshness(o, now)
    geo = eff.get("geography") or []
    elig = eff.get("eligibility") or {}
    d = {
        "campaign_id": o.campaign_id, "brand": o.brand.name, "brand_slug": o.brand.slug, "is_own": o.brand.is_own, "title": o.title, "headline": o.headline,
        "status": o.status, "status_label": STATUS_LABELS.get(o.status, o.status), "status_reason": o.status_reason, "status_set_by_person": o.status_set_by_human,
        "needs_review": o.needs_review, "review_reasons": o.review_reasons,
        "freshness": fr["state"], "last_success_check_at": fr["last_success_check_at"], "last_failed_attempt_at": fr["last_failed_attempt_at"], "failure_reason": fr["failure_reason"],
        "first_observed_at": o.first_observed_at, "is_baseline": o.is_baseline, "last_observed_at": o.last_observed_at,
        "published_start": o.published_start, "published_expiry": o.published_expiry, "expiry_text": o.expiry_text,
        "expiry_label": o.published_expiry.isoformat() if o.published_expiry else "Expiry not published",
        "expiry_timezone": o.expiry_timezone, "expiry_timezone_assumed": bool(eff.get("expiry_timezone_assumed")),
        "source_url": o.source_url, "terms_url": o.terms_url,
        "geography": geo, "geography_label": "; ".join(f"{g.get('name') or g.get('state')} ({g.get('level')}{', page context' if g.get('basis') == 'page_context' else ''})" for g in geo) or "Geography not stated",
        "geography_unknown": not geo,
        "states": sorted({g["state"] for g in geo if g.get("state")}),
        "mechanisms": [{"type": m.mechanism_type, "type_label": MECHANISM_LABELS.get(m.mechanism_type, m.mechanism_type), "label": m.label,
                        "amount": _money(m.amount), "qualifier": m.amount_qualifier, "kind": m.amount_kind, "customer_payment_required": _money(m.customer_payment_required),
                        "builder_contribution": _money(m.builder_contribution), "government_component": m.government_component,
                        "tax_basis": m.tax_basis, "time_promise_type": m.time_promise_type, "time_promise_days": m.time_promise_days, "excerpt": m.excerpt} for m in o.mechanisms],
        "ranges": elig.get("ranges") or [], "designs": elig.get("designs") or [], "buyer_types": elig.get("buyer_types") or [],
        "land_status": elig.get("land_status"), "contract_condition": elig.get("contract_condition"),
        "finance_requirements": elig.get("finance_requirements") or [], "exclusions": elig.get("exclusions") or [],
        "conflicts": o.conflicts or [], "corrected_fields": eff.get("corrected_fields") or [],
    }
    d["value_summary"] = value_summary(d["mechanisms"])
    d["key_conditions"] = "; ".join(filter(None, [d["contract_condition"], *(d["finance_requirements"][:2]), d["land_status"], *(d["exclusions"][:1])])) or "Conditions not extracted"
    if detail:
        d["versions"] = [{"version_no": v.version_no, "observed_at": v.observed_at, "method": v.extraction_method, "model": v.model, "extraction_version": v.extraction_version,
                          "is_baseline": v.is_baseline, "diff": v.diff, "snapshot_ids": v.snapshot_ids, "unresolved": v.unresolved} for v in o.versions]
        d["evidence"] = o.versions[-1].evidence if o.versions else []
        d["unresolved"] = o.versions[-1].unresolved if o.versions else []
        d["sources"] = [{"url": s.source.url, "role": s.role, "last_seen_at": s.last_seen_at, "last_outcome": s.source.last_outcome, "last_success_at": s.source.last_success_at,
                         "snapshot_id": s.last_seen_snapshot_id} for s in o.sources]
        d["machine_fields"] = o.versions[-1].fields if o.versions else None
    return d


def value_summary(mechs: list[dict]) -> str:
    parts = []
    for m in mechs:
        if m["amount"] is not None:
            q = {"up_to": "Up to ", "from": "From "}.get(m["qualifier"] or "", "")
            kind = {"cash_saving": "cash saving", "inclusion_value": "claimed inclusion value", "payment_arrangement": "payment arrangement", "government_grant": "government grant"}.get(m["kind"] or "", "")
            parts.append(f"{q}${m['amount']:,.0f}{' ' + kind if kind else ''}")
        elif m["time_promise_days"]:
            when = {"start_of_construction": "to construction start", "completion": "to completion"}.get(m["time_promise_type"] or "", "(timing unclear)")
            parts.append(f"{m['time_promise_days']} days {when}")
        else:
            parts.append("Value not published")
        if m["customer_payment_required"] is not None:
            parts.append(f"customer pays ${m['customer_payment_required']:,.0f}")
    return "; ".join(parts) or "Value not published"


def list_offers(db: Session, *, state: str | None = None, brands: list[str] | None = None, benefit_types: list[str] | None = None, statuses: list[str] | None = None,
                range_q: str | None = None, buyer_q: str | None = None, current_only: bool = False, include_unknown_geo: bool = True,
                as_of: date | None = None) -> list[Offer]:
    q = select(Offer).options(selectinload(Offer.mechanisms), selectinload(Offer.applicability), selectinload(Offer.brand), selectinload(Offer.versions), selectinload(Offer.sources))
    if brands:
        q = q.join(Brand, Brand.id == Offer.brand_id).where(Brand.slug.in_(brands))
    if statuses:
        q = q.where(Offer.status.in_(statuses))
    if current_only:
        q = q.where(Offer.status.in_(["advertised", "needs_review"]))
    if benefit_types:
        q = q.where(Offer.id.in_(select(OfferMechanism.offer_id).where(OfferMechanism.mechanism_type.in_(benefit_types))))
    offers = list(db.scalars(q.order_by(Offer.brand_id, Offer.id)).unique())
    if as_of:  # historical view: offers that had been observed by then and not yet expired
        offers = [o for o in offers if o.first_observed_at.date() <= as_of and (o.published_expiry is None or o.published_expiry >= as_of)]
    out = []
    brand_states: dict[int, set[str]] = defaultdict(set)
    if state:
        for r in db.scalars(select(BrandRegion)):
            brand_states[r.brand_id].add(r.state)
    for o in offers:
        if state:
            states = {a.state for a in o.applicability if a.state}
            if state in states:
                pass
            elif not o.applicability and include_unknown_geo and state in brand_states[o.brand_id]:
                pass  # geography unknown: shown, but flagged 'Geography not stated'
            else:
                continue
        eff = o.effective or {}
        elig = eff.get("eligibility") or {}
        if range_q and not any(range_q.lower() in x.lower() for x in (elig.get("ranges") or []) + (elig.get("designs") or [])):
            continue
        if buyer_q and not any(buyer_q.lower() in x.lower() for x in (elig.get("buyer_types") or [])):
            continue
        out.append(o)
    return out


def promo_patterns(db: Session, state: str | None, days: int = 30) -> dict:
    """Mechanisms across the monitored roster, with counts, and strengthened / extended / removed signals. Not a market census."""
    offers = list_offers(db, state=state, include_unknown_geo=False)
    now = now_utc()
    since = now - timedelta(days=days)
    roster = _roster_brands(db, state)
    by_mech: dict[str, dict] = {}
    for o in offers:
        if o.status not in ("advertised", "needs_review"):
            continue
        for m in o.mechanisms:
            e = by_mech.setdefault(m.mechanism_type, {"type": m.mechanism_type, "label": MECHANISM_LABELS.get(m.mechanism_type, m.mechanism_type), "offers": 0, "brands": set(), "strengthened": 0, "extended": 0, "removed": 0})
            e["offers"] += 1
            e["brands"].add(o.brand.name)
    for o in offers:
        for v in o.versions:
            if v.observed_at < since or v.version_no == 1:
                continue
            for d in v.diff:
                f = d["field"]
                if f.startswith("benefit["):
                    b, a = d.get("before") or {}, d.get("after") or {}
                    mt = f.split("[")[1].split(":")[0]
                    if isinstance(b, dict) and isinstance(a, dict) and (a.get("amount") or 0) > (b.get("amount") or 0):
                        by_mech.setdefault(mt, {"type": mt, "label": MECHANISM_LABELS.get(mt, mt), "offers": 0, "brands": set(), "strengthened": 0, "extended": 0, "removed": 0})["strengthened"] += 1
                if f == "dates.expiry_date" and d.get("before") and d.get("after") and d["after"] > d["before"]:
                    for m in o.mechanisms:
                        by_mech.setdefault(m.mechanism_type, {"type": m.mechanism_type, "label": MECHANISM_LABELS.get(m.mechanism_type, m.mechanism_type), "offers": 0, "brands": set(), "strengthened": 0, "extended": 0, "removed": 0})["extended"] += 1
        if o.status in ("no_longer_observed", "expired") and o.last_observed_at and o.last_observed_at >= since:
            for m in o.mechanisms:
                by_mech.setdefault(m.mechanism_type, {"type": m.mechanism_type, "label": MECHANISM_LABELS.get(m.mechanism_type, m.mechanism_type), "offers": 0, "brands": set(), "strengthened": 0, "extended": 0, "removed": 0})["removed"] += 1
    covered = {o.brand.slug for o in offers if o.status in ("advertised", "needs_review")}
    mech_list = sorted(({**e, "brands": sorted(e["brands"])} for e in by_mech.values()), key=lambda e: -e["offers"])
    return {"state": state or "AU", "window_days": days, "mechanisms": mech_list, "roster_size": len(roster), "roster_with_current_offers": len(covered),
            "roster_missing": sorted(b.name for b in roster if b.slug not in covered),
            "note": "Counts cover the monitored roster only - not every Australian builder - and only offers captured from the monitored pages."}


def _roster_brands(db: Session, state: str | None) -> list[Brand]:
    q = select(Brand).where(Brand.active.is_(True))
    if state in STATES:
        q = q.join(BrandRegion, BrandRegion.brand_id == Brand.id).where(BrandRegion.state == state)
    return list(db.scalars(q.order_by(Brand.is_own.desc(), Brand.name)).unique())


# --------------------------------------------------------------------------- source health
def source_health(db: Session) -> list[dict]:
    s = get_settings()
    now = now_utc()
    out = []
    for src in db.scalars(select(Source).order_by(Source.brand_id, Source.id)):
        age = (now - src.last_success_at).total_seconds() / 3600 if src.last_success_at else None
        out.append({"id": src.id, "brand": src.brand.name if src.brand else None, "kind": src.kind, "url": src.url, "state": src.state, "active": src.active,
                    "last_attempt_at": src.last_attempt_at, "last_success_at": src.last_success_at, "last_outcome": src.last_outcome, "last_error": src.last_error,
                    "consecutive_failures": src.consecutive_failures, "freshness": "never_collected" if age is None else ("stale" if age > s.stale_after_hours or src.last_outcome != "ok" else "fresh"),
                    "notes": src.notes})
    return out


# --------------------------------------------------------------------------- trends
def trend_capabilities(db: Session) -> list[dict]:
    rows = {c.geo_key: c for c in db.scalars(select(TrendCapability))}
    out = []
    for gk in ["AU", "VIC", "NSW", "QLD", "SA", "city:Melbourne", "city:Sydney", "city:Brisbane", "city:Adelaide"]:
        c = rows.get(gk)
        out.append({"geo": gk, "level": "country" if gk == "AU" else "city" if gk.startswith("city:") else "state", "tested": bool(c and c.tested_at),
                    "supported": c.supported if c else None, "coverage": c.coverage if c else None, "location_name": c.location_name if c else None,
                    "tested_at": c.tested_at if c else None, "detail": c.detail if c else {}})
    return out


def _series_payload(db: Session, req: TrendRequest, kw: str) -> dict:
    ck = clean_keyword(kw)
    obs = list(db.scalars(select(TrendObservation).where(TrendObservation.request_id == req.id, TrendObservation.keyword == ck).order_by(TrendObservation.period_start)))
    mets = {m.metric_key: m for m in db.scalars(select(TrendMetric).where(TrendMetric.request_id == req.id, TrendMetric.keyword == ck))}
    pts = [M.Point(o.period_start, o.period_end, o.value, o.is_partial) for o in obs]
    def met(k):
        m = mets.get(k)
        if not m:
            return {"status": "unavailable", "reason": "Not computed", "label": "Insufficient search data"}
        return {"status": m.status, "value_pct": m.value, "current_avg": m.current_avg, "comparison_avg": m.comparison_avg, "reason": m.reason,
                "label": M.READABLE_REASONS.get(m.reason or "", None) if m.status != "ok" else None, "windows": {"current": (m.details or {}).get("current_window"), "comparison": (m.details or {}).get("comparison_window")}}
    return {"keyword": kw, "points": [{"date": o.period_start.isoformat(), "end": o.period_end.isoformat() if o.period_end else None, "value": o.value, "partial": o.is_partial} for o in obs],
            "sparkline_individually_normalised": M.normalise_for_sparkline(pts), "mom4": met("mom4"), "yoy4": met("yoy4")}


def _request_meta(req: TrendRequest) -> dict:
    return {"request_id": req.id, "retrieved_at": req.requested_at, "geo": req.geo_key, "geo_level": req.geo_level, "location_name": req.location_name,
            "location_code": req.location_code, "search_type": req.search_type, "category_code": req.category_code, "date_from": req.date_from, "date_to": req.date_to,
            "provider": req.provider, "cost_reported": float(req.cost_reported) if req.cost_reported is not None else None}


def geo_view_label(geo: str) -> str:
    return {"AU": "Australia (national Google Trends data)", "VIC": "Victoria (statewide)", "NSW": "New South Wales (statewide)", "QLD": "Queensland (statewide)", "SA": "South Australia (statewide)"}.get(geo, geo)


def brand_trend_groups(db: Session, geo: str) -> dict:
    """Comparable groups only: each group is one request (same geography/window/type/category). No cross-group ranking."""
    groups = []
    plans = list(db.scalars(select(TrendPlan).where(TrendPlan.geo_key == geo, TrendPlan.purpose == "brands").order_by(TrendPlan.id)))
    brands = {b.id: b for b in db.scalars(select(Brand))}
    for plan in plans:
        req = pipeline_latest_request(db, plan)
        if req is None:
            groups.append({"plan_key": plan.plan_key, "active": plan.active, "status": "no_data", "keywords": plan.keywords, "brands": [brands[i].name for i in plan.brand_ids if i in brands]})
            continue
        series = [_series_payload(db, req, kw) for kw in req.keywords]
        by_kw = {clean_keyword(k): (brands[i].name if i in brands else k) for k, i in zip(plan.keywords, plan.brand_ids)}
        for s in series:
            s["brand"] = by_kw.get(clean_keyword(s["keyword"]), s["keyword"])
        groups.append({"plan_key": plan.plan_key, "active": plan.active, "status": "ok", "meta": _request_meta(req), "series": series,
                       "scale_note": "Values are on one shared 0-100 scale within this group only. Do not compare across groups."})
    return {"geo": geo, "geo_label": geo_view_label(geo), "groups": groups,
            "note": "Google Trends values are relative search-interest indices, not search volumes, enquiries, sales or market share."}


def pipeline_latest_request(db: Session, plan: TrendPlan) -> TrendRequest | None:
    return db.scalar(select(TrendRequest).where(TrendRequest.kind == "compare", TrendRequest.geo_key == plan.geo_key, TrendRequest.status == "ok",
                                                TrendRequest.keywords == plan.keywords).order_by(TrendRequest.id.desc()).limit(1))


def theme_trends(db: Session, geo: str) -> list[dict]:
    out = []
    for plan in db.scalars(select(TrendPlan).where(TrendPlan.geo_key == geo, TrendPlan.purpose == "theme").order_by(TrendPlan.id)):
        theme = db.scalar(select(Theme).where(Theme.slug == plan.theme_slug))
        req = pipeline_latest_request(db, plan)
        entry = {"theme": plan.theme_slug, "theme_name": theme.name if theme else plan.theme_slug, "active": plan.active, "keywords": plan.keywords, "geo": geo, "geo_label": geo_view_label(geo)}
        if req is None:
            entry.update(status="no_data", series=[])
        else:
            entry.update(status="ok", meta=_request_meta(req), series=[_series_payload(db, req, kw) for kw in req.keywords],
                         note="Keyword series share one scale within this theme group. They are not averaged into a market-demand total.")
        out.append(entry)
    return out


def trend_overview_for_brief(db: Session, geo: str) -> list[dict]:
    """Compact computed facts for the brief generator."""
    facts = []
    for g in brand_trend_groups(db, geo)["groups"]:
        if g["status"] != "ok":
            continue
        for s in g["series"]:
            for mk in ("mom4", "yoy4"):
                m = s[mk]
                if m["status"] == "ok":
                    facts.append({"id": f"trend:{g['meta']['request_id']}:{s['keyword']}:{mk}", "kind": "brand_search_interest", "brand": s.get("brand"), "keyword": s["keyword"], "geo": geo,
                                  "metric": "latest 4 complete weeks vs preceding 4" if mk == "mom4" else "latest 4 complete weeks vs same weeks a year earlier",
                                  "change_pct": m["value_pct"], "current_avg_index": m["current_avg"], "comparison_avg_index": m["comparison_avg"], "retrieved": g["meta"]["retrieved_at"].date().isoformat()})
    for t in theme_trends(db, geo):
        if t["status"] != "ok":
            continue
        for s in t["series"]:
            m = s["mom4"]
            if m["status"] == "ok":
                facts.append({"id": f"trend:{t['meta']['request_id']}:{s['keyword']}:mom4", "kind": "theme_keyword_search_interest", "theme": t["theme_name"], "keyword": s["keyword"], "geo": geo,
                              "metric": "latest 4 complete weeks vs preceding 4", "change_pct": m["value_pct"], "current_avg_index": m["current_avg"], "comparison_avg_index": m["comparison_avg"]})
    return facts


# --------------------------------------------------------------------------- GSC / themes
def gsc_overview(db: Session) -> dict:
    imp = db.scalar(select(GscImport).order_by(GscImport.id.desc()).limit(1))
    if not imp:
        return {"imported": False}
    from . import gsc as G

    return {"imported": True, "import": {"id": imp.id, "file_name": imp.file_name, "reported_period_start": imp.reported_period_start, "reported_period_end": imp.reported_period_end,
                                           "period_note": imp.period_note, "imported_at": imp.imported_at, "query_rows": imp.query_row_count, "page_rows": imp.page_row_count},
            "themes": G.theme_summary(db, imp.id), "pages": G.metricon_page_targets(db, imp.id),
            "caveat": "Google Search Console measures Metricon's own search visibility only. It is not total market demand, competitor demand, sales, visitor location or a trend over time."}


def events_list(db: Session, *, state: str | None = None, brands: list[str] | None = None, event_type: str | None = None, days: int = 60, include_baseline: bool = False) -> list[dict]:
    since = now_utc() - timedelta(days=days)
    q = select(CompetitorEvent).where(CompetitorEvent.observed_at >= since).order_by(CompetitorEvent.observed_at.desc())
    if state:
        q = q.where(or_(CompetitorEvent.state == state, CompetitorEvent.state.is_(None)))
    if event_type:
        q = q.where(CompetitorEvent.event_type == event_type)
    if brands:
        q = q.join(Brand, Brand.id == CompetitorEvent.brand_id).where(Brand.slug.in_(brands))
    out = []
    for e in db.scalars(q):
        out.append({"id": e.id, "event_key": e.event_key, "brand": e.brand.name, "brand_slug": e.brand.slug, "event_type": e.event_type, "state": e.state, "title": e.title, "summary": e.summary,
                    "theme": e.theme_slug, "observed_at": e.observed_at, "effective_date": e.effective_date, "source_url": e.source_url, "before": e.before, "after": e.after,
                    "evidence": e.evidence, "offer_campaign_id": e.offer.campaign_id if e.offer_id and e.offer else None,
                    "date_note": "First observed by our crawler - not a confirmed launch date." if not e.effective_date else "Effective date stated by the source."})
    return out
