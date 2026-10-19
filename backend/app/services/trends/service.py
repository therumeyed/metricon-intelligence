"""Trend collection: location resolution, capability testing, cached comparable requests, stored observations and metrics."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import (
    Brand,
    BrandAlias,
    BrandRegion,
    RelatedQueryCandidate,
    Theme,
    TrendCapability,
    TrendLocation,
    TrendMetric,
    TrendObservation,
    TrendPlan,
    TrendRequest,
)
from ...timeutil import now_utc, today_local
from .. import spend
from . import metrics as M
from .provider import DataForSEOTrends, ExploreResult, ProviderError, clean_keyword

STATE_NAMES = {"VIC": "Victoria", "NSW": "New South Wales", "QLD": "Queensland", "SA": "South Australia"}
CITY_NAMES = {"VIC": "Melbourne", "NSW": "Sydney", "QLD": "Brisbane", "SA": "Adelaide"}
GEO_KEYS = ["AU", "VIC", "NSW", "QLD", "SA"]
HISTORY_DAYS = 735  # ~24 months collected; 12 months shown by default
DEFAULT_TYPE = "web"


# --------------------------------------------------------------------------- locations + capability
def sync_locations(db: Session, client: DataForSEOTrends) -> int:
    rows = client.locations("au")
    n = 0
    have = {(l.provider, l.location_code): l for l in db.scalars(select(TrendLocation))}
    for r in rows:
        code = r.get("location_code")
        if code is None:
            continue
        loc = have.get(("dataforseo_google_trends", code))
        if loc is None:
            loc = TrendLocation(location_code=code)
            db.add(loc)
            n += 1
        loc.location_name, loc.location_type = r.get("location_name") or "", r.get("location_type")
        loc.parent_code, loc.country_iso, loc.retrieved_at = r.get("location_code_parent"), r.get("country_iso_code"), now_utc()
    db.flush()
    return n


def resolve_location(db: Session, geo_key: str) -> TrendLocation | None:
    """Resolve from the stored catalogue by name/type. Nothing is hardcoded."""
    locs = list(db.scalars(select(TrendLocation).where(TrendLocation.country_iso == "AU")))
    if geo_key == "AU":
        return next((l for l in locs if l.location_name.strip().lower() == "australia" and (l.location_type or "").lower() == "country"), None) or \
            next((l for l in locs if l.location_name.strip().lower() == "australia"), None)
    if geo_key in STATE_NAMES:
        want = STATE_NAMES[geo_key].lower()
        cands = [l for l in locs if l.location_name.split(",")[0].strip().lower() == want and (l.location_type or "").lower() in ("state", "region", "territory")]
        return cands[0] if cands else None
    if geo_key.startswith("city:"):
        want = geo_key[5:].lower()
        cands = [l for l in locs if l.location_name.split(",")[0].strip().lower() == want and (l.location_type or "").lower() in ("city", "dma region", "municipality")]
        return cands[0] if cands else None
    return None


def geo_level(geo_key: str) -> str:
    return "country" if geo_key == "AU" else "city" if geo_key.startswith("city:") else "state"


def geo_label(geo_key: str) -> str:
    return "Australia" if geo_key == "AU" else STATE_NAMES.get(geo_key, geo_key.replace("city:", ""))


def _cap_row(db: Session, gk: str) -> TrendCapability:
    cap = db.scalar(select(TrendCapability).where(TrendCapability.geo_key == gk))
    if cap is None:
        cap = TrendCapability(geo_key=gk, geo_level=geo_level(gk), detail={})
        db.add(cap)
    return cap


def record_capability(db: Session, geo_key: str, *, req: TrendRequest | None = None, unsupported_reason: str | None = None) -> None:
    """Every real request teaches us what the provider supports for a geography, so the coverage table never depends on a manual test."""
    cap = _cap_row(db, geo_key)
    cap.tested_at = now_utc()
    if unsupported_reason:
        cap.supported, cap.coverage, cap.detail = False, "none", {"reason": unsupported_reason, "source": "weekly refresh"}
        return
    if req is None:
        return
    obs = list(db.scalars(select(TrendObservation).where(TrendObservation.request_id == req.id)))
    vals = [o.value for o in obs if o.value is not None and not o.is_partial]
    nz = sum(1 for v in vals if v > 0)
    share = nz / len(vals) if vals else 0
    cap.location_code, cap.location_name = req.location_code, req.location_name
    cap.supported = bool(vals)
    cap.coverage = "none" if not vals or share == 0 else "full" if share >= 0.8 else "sparse"
    cap.detail = {"points": len(vals), "nonzero_share": round(share, 2), "request_id": req.id, "source": "weekly refresh",
                  "keywords": req.keywords, "cost_reported": float(req.cost_reported) if req.cost_reported is not None else None}


def capability_test(db: Session, client: DataForSEOTrends, geo_keys: list[str], job_run_id: int | None = None) -> list[dict]:
    """Real provider test per geography: does it resolve, and does a generic home-building query return usable data?"""
    results = []
    for gk in geo_keys:
        cap = db.scalar(select(TrendCapability).where(TrendCapability.geo_key == gk)) or TrendCapability(geo_key=gk, geo_level=geo_level(gk))
        db.add(cap)
        loc = resolve_location(db, gk)
        cap.tested_at = now_utc()
        if loc is None:
            cap.supported, cap.coverage, cap.location_code, cap.location_name = False, "none", None, None
            cap.detail = {"reason": "location not present in the provider's Google Trends location catalogue for AU"}
        else:
            cap.location_code, cap.location_name = loc.location_code, loc.location_name
            try:
                req = fetch_compare(db, client, ["home builders", "display homes"], gk, days=365, purpose="capability", job_run_id=job_run_id, use_cache=False)
                obs = list(db.scalars(select(TrendObservation).where(TrendObservation.request_id == req.id)))
                vals = [o.value for o in obs if o.value is not None]
                nz = sum(1 for v in vals if v > 0)
                share = nz / len(vals) if vals else 0
                cap.supported = bool(vals)
                cap.coverage = "none" if not vals or share == 0 else "full" if share >= 0.8 else "sparse"
                cap.detail = {"points": len(vals), "nonzero_share": round(share, 2), "request_id": req.id, "cost_reported": float(req.cost_reported) if req.cost_reported is not None else None}
            except (ProviderError, spend.BudgetExceeded) as e:
                cap.supported, cap.coverage, cap.detail = None, None, {"reason": f"test failed: {e}"}
        db.flush()
        results.append({"geo": gk, "supported": cap.supported, "coverage": cap.coverage, "location": cap.location_name, "detail": cap.detail})
    return results


# --------------------------------------------------------------------------- comparable requests
def request_hash(keywords: list[str], geo_key: str, location_code: int | None, date_from: date, date_to: date, search_type: str, category: int, kind: str) -> str:
    payload = {"kw": [clean_keyword(k).lower() for k in sorted(keywords)], "geo": geo_key, "loc": location_code, "from": date_from.isoformat(),
               "to": date_to.isoformat(), "type": search_type, "cat": category, "kind": kind}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _cached(db: Session, h: str, max_age_hours: int) -> TrendRequest | None:
    cutoff = now_utc() - timedelta(hours=max_age_hours)
    return db.scalar(select(TrendRequest).where(TrendRequest.request_hash == h, TrendRequest.status == "ok", TrendRequest.requested_at >= cutoff)
                     .order_by(TrendRequest.id.desc()).limit(1))


def fetch_compare(db: Session, client: DataForSEOTrends | None, keywords: list[str], geo_key: str, *, days: int = HISTORY_DAYS, search_type: str = DEFAULT_TYPE,
                  category: int = 0, purpose: str = "plan", theme_slug: str | None = None, job_run_id: int | None = None, use_cache: bool = True,
                  requests_so_far: int = 0, kind: str = "compare") -> TrendRequest:
    """One comparable request: same geography, window, type and category for every keyword in it (<=5)."""
    if len(keywords) > 5:
        raise ProviderError("at most 5 keywords per comparison")
    s = get_settings()
    loc = resolve_location(db, geo_key)
    if loc is None:
        record_capability(db, geo_key, unsupported_reason="location not present in the provider's Google Trends catalogue for Australia")
        raise ProviderError(f"{geo_key}: location not in provider catalogue (run capability test / sync locations)")
    today = today_local()
    d_to, d_from = today, today - timedelta(days=days)
    h = request_hash(keywords, geo_key, loc.location_code, d_from, d_to, search_type, category, kind)
    if use_cache:
        hit = _cached(db, h, s.trend_cache_hours)
        if hit is not None:
            return hit
    if client is None:
        raise ProviderError("DataForSEO not connected")
    spend.check_dataforseo(db, s.dataforseo_est_cost_per_explore, requests_so_far)
    item_types = ["google_trends_queries_list"] if kind == "discover" else ["google_trends_graph"]
    req = TrendRequest(request_hash=h, kind=kind, keywords=list(keywords), geo_key=geo_key, geo_level=geo_level(geo_key), location_code=loc.location_code,
                       location_name=loc.location_name, search_type=search_type, category_code=category, date_from=d_from, date_to=d_to,
                       settings={"item_types": item_types, "language": "en"}, purpose=purpose, theme_slug=theme_slug, job_run_id=job_run_id)
    db.add(req)
    db.flush()
    try:
        res = client.explore(keywords=keywords, location_code=loc.location_code, date_from=d_from.isoformat(), date_to=d_to.isoformat(),
                             search_type=search_type, category_code=category, item_types=item_types)
    except ProviderError as e:
        req.status, req.error = "failed", str(e)[:500]  # a failed request produces no observations - never zeros
        db.flush()
        raise
    req.raw_response = res.raw
    req.cost_reported = Decimal(str(res.cost)) if res.cost is not None else None
    req.cost_estimated = Decimal(str(s.dataforseo_est_cost_per_explore))
    spend.record(db, "dataforseo", kind="estimated", amount=s.dataforseo_est_cost_per_explore, job_run_id=job_run_id, detail={"request_id": req.id})
    if res.cost is not None:
        spend.record(db, "dataforseo", kind="reported", amount=res.cost, calls=0, job_run_id=job_run_id, detail={"request_id": req.id})
    if kind == "compare":
        store_observations(db, req, res)
        compute_and_store_metrics(db, req)
        db.flush()
        if purpose != "capability":
            record_capability(db, geo_key, req=req)
    else:
        store_related(db, req, res, keywords[0])
    db.flush()
    return req


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def store_observations(db: Session, req: TrendRequest, res: ExploreResult) -> int:
    n = 0
    for kw, pts in res.graph.items():
        seen = set()
        for p in pts:
            start = _d(p["start"])
            if start is None or start in seen:
                continue
            seen.add(start)
            v = p["value"]
            db.add(TrendObservation(request_id=req.id, keyword=kw, period_start=start, period_end=_d(p["end"]),
                                    value=int(v) if isinstance(v, (int, float)) else None, is_partial=bool(p["missing"])))
            n += 1
    return n


def series_for(db: Session, req_id: int, keyword: str) -> list[M.Point]:
    rows = db.scalars(select(TrendObservation).where(TrendObservation.request_id == req_id, TrendObservation.keyword == keyword).order_by(TrendObservation.period_start))
    return [M.Point(r.period_start, r.period_end, r.value, r.is_partial) for r in rows]


def compute_and_store_metrics(db: Session, req: TrendRequest, today: date | None = None) -> None:
    today = today or today_local()
    for old in db.scalars(select(TrendMetric).where(TrendMetric.request_id == req.id)):
        db.delete(old)
    db.flush()
    for kw in req.keywords:
        pts = series_for(db, req.id, clean_keyword(kw))
        for key, fn in (("mom4", M.four_week_change), ("yoy4", M.year_on_year)):
            ch = fn(pts, today)
            db.add(TrendMetric(request_id=req.id, keyword=clean_keyword(kw), metric_key=key, status=ch.status, value=ch.value_pct, current_avg=ch.current_avg,
                               comparison_avg=ch.comparison_avg, reason=ch.reason, details=ch.as_dict()))


def store_related(db: Session, req: TrendRequest, res: ExploreResult, seed: str) -> int:
    n = 0
    for kind in ("rising", "top"):
        for q in (res.queries.get(kind) or [])[:25]:
            text = (q.get("query") or "").strip().lower()
            if not text:
                continue
            if db.scalar(select(RelatedQueryCandidate.id).where(RelatedQueryCandidate.seed == seed, RelatedQueryCandidate.geo_key == req.geo_key, RelatedQueryCandidate.query == text)):
                continue
            rel, why = is_relevant_query(text)
            db.add(RelatedQueryCandidate(request_id=req.id, seed=seed, geo_key=req.geo_key, query=text[:200], kind=kind, value=q.get("value"), relevant=rel,
                                         review_status="proposed" if rel else "rejected", suggested_theme_slug=suggest_theme(db, text) if rel else None))
            n += 1
    return n


# --------------------------------------------------------------------------- discovery noise filter
_INTENT = re.compile(r"\b(home|homes|house|houses|builder|builders|build|building|duplex|dual occupancy|townhouse|townhome|knock ?down|rebuild|acreage|display|floor ?plans?|facade|land package|house and land|granny|first home)\b", re.I)
_NOISE = re.compile(r"\b(rent|rental|rentals|for rent|real estate|realestate|bunnings|ikea|paint|curtain|jobs?|salary|lego|sims|minecraft|roblox|bloxburg|game|movie|tv|show|series|celebrity|mansion|airbnb|insurance|cleaning|plumber|electrician|garden|furniture|stock|share price|asx)\b", re.I)


def is_relevant_query(q: str) -> tuple[bool, str]:
    if _NOISE.search(q):
        return False, "noise term"
    if not _INTENT.search(q):
        return False, "no home-building intent"
    return True, "ok"


def suggest_theme(db: Session, q: str) -> str | None:
    from ..gsc import Mapper

    m = Mapper(db)
    tid, method, excl = m.map(q)
    if tid is None:
        return None
    return db.scalar(select(Theme.slug).where(Theme.id == tid))


# --------------------------------------------------------------------------- plans
def brand_term(db: Session, brand: Brand) -> str:
    alias = db.scalar(select(BrandAlias.alias).where(BrandAlias.brand_id == brand.id, BrandAlias.kind == "search_term").order_by(BrandAlias.id).limit(1))
    return alias or brand.name.lower()


BROAD_SEEDS = ["home builders", "house and land packages", "display homes", "knockdown rebuild", "dual occupancy"]


def discovery_seeds(db: Session) -> list[str]:
    """Broad market seeds plus the head term of every active theme, so 'what are people newly searching for' covers each theme."""
    seeds = list(BROAD_SEEDS)
    for t in db.scalars(select(Theme).where(Theme.active.is_(True)).order_by(Theme.sort_order)):
        kws = sorted((k for k in t.keywords if len(k.keyword.split()) <= 4), key=lambda k: (-(k.gsc_impressions or 0), not k.tracked))
        if kws and kws[0].keyword not in seeds:
            seeds.append(kws[0].keyword)
    return seeds


def build_default_plans(db: Session) -> int:
    """Create (never overwrite) refresh plans. Pilot plans are active; the rest are created inactive until an admin enables them."""
    created = 0
    metricon = db.scalar(select(Brand).where(Brand.is_own.is_(True)))
    own_term = brand_term(db, metricon)

    def add(key, purpose, geo, kws, theme=None, brand_ids=None, pilot=False, active=False):
        nonlocal created
        if db.scalar(select(TrendPlan.id).where(TrendPlan.plan_key == key)):
            return
        db.add(TrendPlan(plan_key=key, kind="compare", purpose=purpose, geo_key=geo, keywords=kws, theme_slug=theme, brand_ids=brand_ids or [], pilot=pilot, active=active or pilot))
        created += 1

    regions = list(db.scalars(select(BrandRegion)))
    brands = {b.id: b for b in db.scalars(select(Brand))}
    for st in STATE_NAMES:
        comp = [brands[r.brand_id] for r in regions if r.state == st and not brands[r.brand_id].is_own]
        for i in range(0, len(comp), 4):
            group = comp[i:i + 4]
            add(f"brands:{st}:{i // 4 + 1}", "brands", st, [own_term] + [brand_term(db, b) for b in group], brand_ids=[metricon.id] + [b.id for b in group], pilot=(st == "VIC" and i == 0))
    # national: the brands with the widest monitored footprint (actual national Trends data, not an average of states)
    spread = sorted((b for b in brands.values() if not b.is_own), key=lambda b: -sum(1 for r in regions if r.brand_id == b.id))[:4]
    add("brands:AU:1", "brands", "AU", [own_term] + [brand_term(db, b) for b in spread], brand_ids=[metricon.id] + [b.id for b in spread], pilot=True)
    themes = list(db.scalars(select(Theme).where(Theme.active.is_(True)).order_by(Theme.sort_order)))
    for n, t in enumerate(themes):
        kws = [k.keyword for k in sorted((k for k in t.keywords if k.tracked), key=lambda k: -(k.gsc_impressions or 0))][:5]
        if len(kws) < 2:
            continue
        for geo in GEO_KEYS:
            add(f"theme:{t.slug}:{geo}", "theme", geo, kws, theme=t.slug, pilot=(geo in ("AU", "VIC") and n < 4))
    return created


def refresh_plans(db: Session, client: DataForSEOTrends, job_run_id: int | None = None, only_pilot: bool = False, limit: int | None = None) -> dict:
    out = {"requested": 0, "cached": 0, "failed": [], "skipped_budget": 0}
    q = select(TrendPlan).where(TrendPlan.active.is_(True), TrendPlan.kind == "compare")
    if only_pilot:
        q = q.where(TrendPlan.pilot.is_(True))
    for plan in db.scalars(q.order_by(TrendPlan.pilot.desc(), TrendPlan.id)):
        if limit is not None and out["requested"] >= limit:
            break
        try:
            before = db.scalar(select(TrendRequest.id).order_by(TrendRequest.id.desc()).limit(1)) or 0
            req = fetch_compare(db, client, plan.keywords, plan.geo_key, purpose=plan.purpose, theme_slug=plan.theme_slug, job_run_id=job_run_id, requests_so_far=out["requested"])
            if req.id <= before:
                out["cached"] += 1
            else:
                out["requested"] += 1
            db.commit()
        except spend.BudgetExceeded as e:
            out["skipped_budget"] += 1
            out["budget_message"] = str(e)
            break
        except ProviderError as e:
            db.commit()
            out["failed"].append({"plan": plan.plan_key, "error": str(e)[:200]})
    return out


def latest_request_for_plan(db: Session, plan: TrendPlan) -> TrendRequest | None:
    return db.scalar(select(TrendRequest).where(TrendRequest.kind == "compare", TrendRequest.geo_key == plan.geo_key, TrendRequest.status == "ok",
                                                TrendRequest.keywords == plan.keywords).order_by(TrendRequest.id.desc()).limit(1))
