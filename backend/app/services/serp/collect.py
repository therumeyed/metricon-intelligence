"""Search-results collection: Google results are fetched through the provider and stored like any other source snapshot.

Why: the builders' own sites (Metricon's especially) sit behind bot protection we do not bypass. A search result's title, snippet and URL are
public, legitimately obtained evidence of what a brand is advertising and what is being written about it. It is thinner than the page itself,
so everything derived from it is marked `search_snippet` evidence and a result that stops appearing is never read as "withdrawn".

Snapshot text is built so rank movements and result dates do not change the content hash: results are sorted by URL and carry no positions.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import AppSetting, Brand, BrandRegion, Source, SourceSnapshot
from ...timeutil import now_utc
from .. import spend
from ..content import same_site
from ..trends.provider import ProviderError
from . import SERP_KINDS
from .provider import DataForSEOSerp, SerpResult

log = logging.getLogger("serp")
LOCATION_KEY = "serp_location_au"
STATE_NAMES = {"VIC": "Victoria", "NSW": "New South Wales", "QLD": "Queensland", "SA": "South Australia"}
FALLBACK_AU = {"code": 2036, "name": "Australia", "from_catalogue": False}


def search_name(brand: Brand) -> str:
    return "GJ Gardner Homes" if brand.slug == "gj-gardner" else brand.name


def google_url(query: str, news: bool = False) -> str:
    return f"https://www.google.com.au/search?q={quote_plus(query)}" + ("&tbm=nws" if news else "")


# --------------------------------------------------------------------------- seeding
def planned_searches(db: Session) -> list[dict]:
    """The standing searches: per brand 'offers' and 'promotions'; per state for brands that trade in several; news per brand."""
    plans = []
    for b in db.scalars(select(Brand).where(Brand.active.is_(True)).order_by(Brand.id)):
        name = search_name(b)
        states = [r.state for r in db.scalars(select(BrandRegion).where(BrandRegion.brand_id == b.id))]
        for q in (f"{name} offers", f"{name} current promotions"):
            plans.append({"brand": b, "kind": "serp_offers", "query": q, "state": None})
        if len(states) > 1:
            for st in sorted(states):
                plans.append({"brand": b, "kind": "serp_offers", "query": f"{name} offers {STATE_NAMES.get(st, st)}", "state": st})
        plans.append({"brand": b, "kind": "serp_news", "query": name, "state": None})
    return plans


def seed_search_sources(db: Session) -> dict:
    """Idempotent. Creates missing standing searches; never touches ones an admin edited or switched off."""
    made = 0
    for p in planned_searches(db):
        url = google_url(p["query"], news=p["kind"] == "serp_news")
        if db.scalar(select(Source.id).where(Source.url == url, Source.context_key == "")):
            continue
        ctx = {"search": {"query": p["query"], "engine": "google", "result_kind": "news" if p["kind"] == "serp_news" else "organic",
                          "location": "Australia", "language_code": "en", "state_in_query": p["state"]}}
        db.add(Source(brand_id=p["brand"].id, kind=p["kind"], url=url, context=ctx, fetch_mode="serp",
                      notes="Google results via DataForSEO (not a crawl of the brand's site)"))
        made += 1
    db.flush()
    return {"search_sources": made}


# --------------------------------------------------------------------------- location
def resolve_au_location(db: Session, client: DataForSEOSerp | None) -> dict:
    """Australia-wide location for SERP requests, taken from the provider's catalogue and cached. Falls back to the documented code with a flag."""
    row = db.get(AppSetting, LOCATION_KEY)
    if row is not None and row.value.get("from_catalogue"):
        return row.value
    if client is not None:
        try:
            locs = client.locations("au")
            for l in locs:
                if (l.get("location_type") or "").lower() == "country" and (l.get("location_name") or "").lower() == "australia":
                    val = {"code": int(l["location_code"]), "name": l["location_name"], "from_catalogue": True}
                    if row is None:
                        db.add(AppSetting(key=LOCATION_KEY, value=val))
                    else:
                        row.value = val
                    db.flush()
                    return val
        except ProviderError as e:
            log.warning("SERP location catalogue unavailable: %s", e)
    return FALLBACK_AU


# --------------------------------------------------------------------------- snapshot
def _items_payload(res: SerpResult) -> list[dict]:
    return [{"href": i.url, "text": i.title, "domain": i.domain, "snippet": i.snippet, "publisher": i.publisher,
             "published_at": i.published_at.isoformat() if i.published_at else None} for i in res.items]


def snapshot_text(res: SerpResult, brand: Brand, kind: str) -> tuple[str, list[dict], int]:
    """Readable evidence text. Offers: official-domain results only (a third party's description of an offer is not the offer).
    Returns (text, items kept, number of results dropped)."""
    items = _items_payload(res)
    dropped = 0
    if kind == "serp_offers":
        domains = list(brand.official_domains or [])
        kept = [i for i in items if same_site(i["href"], domains)]
        dropped = len(items) - len(kept)
        items = kept
    items.sort(key=lambda i: i["href"])
    lines = []
    for n, i in enumerate(items, 1):
        lines += [f"{i['text']}", f"Result URL: {i['href']}"] + ([i["snippet"]] if i["snippet"] else []) + [""]
    return "\n".join(lines).strip(), items, dropped


def write_snapshot(db: Session, source: Source, brand: Brand, res: SerpResult, *, location: dict) -> SourceSnapshot:
    now = now_utc()
    text, items, dropped = snapshot_text(res, brand, source.kind)
    h = hashlib.sha256(text.encode()).hexdigest()
    snap = SourceSnapshot(source_id=source.id, requested_url=source.url, final_url=res.check_url or source.url, http_status=200, outcome="ok",
                          content_type="application/x-serp+json", fetched_at=now, fetch_mode="serp", content_hash=h, clean_text=text,
                          title=f"Google results: {res.query}", links=items,
                          context={**(source.context or {}), "location_code": res.location_code or location["code"], "results_total": len(res.items),
                                   "results_kept": len(items), "not_official_domain": dropped, "cost": res.cost})
    prev = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == source.id, SourceSnapshot.outcome == "ok").order_by(SourceSnapshot.id.desc()).limit(1))
    if prev is not None and prev.content_hash == h:
        snap.unchanged_of_id = prev.unchanged_of_id or prev.id
        snap.clean_text, snap.links = None, None
    elif get_settings().store_raw_snapshots:
        snap.raw_gz = gzip.compress(json.dumps(_items_payload(res)).encode())
    source.last_attempt_at = source.last_success_at = now
    source.last_outcome, source.last_error, source.consecutive_failures, source.last_content_hash = "ok", None, 0, h
    db.add(snap)
    db.flush()
    return snap


def record_failure(db: Session, source: Source, error: str, outcome: str = "network") -> None:
    now = now_utc()
    snap = SourceSnapshot(source_id=source.id, requested_url=source.url, outcome=outcome, error=error[:300], fetched_at=now, fetch_mode="serp")
    source.last_attempt_at = source.last_failure_at = now
    source.last_outcome, source.last_error = outcome, error[:300]
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    db.add(snap)
    db.flush()


# --------------------------------------------------------------------------- collection
def est_cost_per_search() -> float:
    return get_settings().dataforseo_est_cost_per_serp


def collect(db: Session, client: DataForSEOSerp, job_run_id: int | None, *, kinds: tuple[str, ...] = SERP_KINDS, limit: int | None = None, brand_slug: str | None = None, source_ids: list[int] | None = None) -> dict:
    s = get_settings()
    out = {"searches": 0, "ok": 0, "results": 0, "errors": [], "spent_estimated": 0.0}
    seed_search_sources(db)
    loc = resolve_au_location(db, client)
    out["location"] = loc
    q = select(Source).where(Source.active.is_(True), Source.kind.in_(kinds)).order_by(Source.id)
    if brand_slug:
        q = q.join(Brand, Brand.id == Source.brand_id).where(Brand.slug == brand_slug)
    if source_ids:
        q = q.where(Source.id.in_(source_ids))
    for src in db.scalars(q):
        if limit is not None and out["searches"] >= limit:
            break
        brand = db.get(Brand, src.brand_id)
        sc = (src.context or {}).get("search") or {}
        query = sc.get("query")
        if not query:
            continue
        try:
            spend.check_dataforseo(db, est_cost_per_search(), out["searches"], max_requests=s.serp_max_requests_per_job)
        except spend.BudgetExceeded as e:
            out["budget_message"] = str(e)
            break
        out["searches"] += 1
        try:
            res = client.search(query=query, kind="news" if sc.get("result_kind") == "news" else "organic", location_code=loc["code"], language_code=sc.get("language_code", "en"))
        except ProviderError as e:
            record_failure(db, src, str(e), "http_error" if e.status_code else "network")
            spend.record(db, "dataforseo", kind="estimated", amount=0, calls=1, job_run_id=job_run_id, detail={"serp": query, "error": str(e)[:120]})
            out["errors"].append({"query": query, "error": str(e)[:200]})
            db.commit()
            continue
        spend.record(db, "dataforseo", kind="estimated", amount=est_cost_per_search(), calls=1, job_run_id=job_run_id, detail={"serp": query})
        if res.cost is not None:
            spend.record(db, "dataforseo", kind="reported", amount=res.cost, calls=1, job_run_id=job_run_id, detail={"serp": query})
        out["spent_estimated"] += est_cost_per_search()
        write_snapshot(db, src, brand, res, location=loc)
        out["ok"] += 1
        out["results"] += len(res.items)
        db.commit()
    return out
