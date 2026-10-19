"""Job functions registered with the runner. Each one isolates its own failures and commits as it goes."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope
from .models import Brand, Offer, Source, SourceSnapshot
from .services import fetcher, llm as llm_mod
from .services import content as content_mod
from .services.jobs import JobContext, job
from .services import events as events_mod
from .services.offers import pipeline
from .timeutil import now_utc

log = logging.getLogger("jobs")
FETCHABLE = ("offer_listing", "offer_detail", "terms", "pdf", "competitor_page", "discovery_seed")
OFFER_LINK_RX = re.compile(r"offer|promo|special|deal|incentive|bonus|campaign|giveaway|price-?(lock|hold)", re.I)


def _fetch_one(source_id: int) -> dict:
    with session_scope() as db:
        src = db.get(Source, source_id)
        snap, doc = fetcher.fetch_source(db, src)
        if snap.outcome != "ok" and src.kind in ("offer_listing", "offer_detail"):
            pipeline.record_failure(db, src, snap)
        return {"id": source_id, "url": src.url, "outcome": snap.outcome, "error": snap.error, "snapshot_id": snap.id}


@job("collect_sources")
def collect_sources(ctx: JobContext) -> dict:
    """Fetch every active official source (bounded concurrency, per-host delay). Evidence is stored before any AI step."""
    s = get_settings()
    only_kinds = ctx.params.get("kinds")
    with session_scope() as db:
        q = select(Source.id, Source.url).where(Source.active.is_(True), Source.kind.in_(only_kinds or FETCHABLE))
        if ctx.params.get("brand"):
            q = q.join(Brand, Brand.id == Source.brand_id).where(Brand.slug == ctx.params["brand"])
        if ctx.params.get("source_ids"):
            q = q.where(Source.id.in_(ctx.params["source_ids"]))
        ids = [r[0] for r in db.execute(q.order_by(Source.id))]
    done = set(ctx.checkpoint.get("done", []))
    results, errors = [], []
    todo = [i for i in ids if i not in done]
    with ThreadPoolExecutor(max_workers=max(1, s.fetch_concurrency)) as ex:
        futs = {ex.submit(_fetch_one, i): i for i in todo}
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001 - one bad source never stops the rest
                errors.append({"source_id": futs[f], "error": f"{type(e).__name__}: {e}"})
                continue
            results.append(r)
            done.add(r["id"])
            if r["outcome"] != "ok":
                errors.append({"source_id": r["id"], "url": r["url"], "outcome": r["outcome"], "error": r["error"]})
            if len(done) % 10 == 0:
                ctx.save_checkpoint(done=sorted(done))
    return {"fetched": len(results), "ok": sum(1 for r in results if r["outcome"] == "ok"), "errors": errors}


def discover_from_seed(db, source: Source, snap: SourceSnapshot) -> int:
    """Register official offer pages found on a brand homepage/seed page (admin can disable any of them)."""
    brand = db.get(Brand, source.brand_id)
    links = fetcher.effective_links(db, snap)
    existing = {s.url.rstrip("/") for s in db.scalars(select(Source).where(Source.brand_id == brand.id))}
    added = 0
    for l in links:
        href = l["href"].split("#")[0].split("?")[0]
        if href.rstrip("/") in existing or not content_mod.same_site(href, list(brand.official_domains or [])):
            continue
        if OFFER_LINK_RX.search(href) or OFFER_LINK_RX.search(l["text"] or ""):
            if re.search(r"\.(jpg|png|svg|webp|css|js)$", href, re.I):
                continue
            if len(href.split("/")) > 7:
                continue
            db.add(Source(brand_id=brand.id, kind="pdf" if href.lower().endswith(".pdf") else "offer_listing", url=href, state=source.state,
                          discovered_from_id=source.id, notes=f"discovered from {source.url}: '{(l['text'] or '')[:60]}'"))
            existing.add(href.rstrip("/"))
            added += 1
            if added >= 8:
                break
    # design / display-home / package / estate index pages become competitor-activity sources (top-level index pages only)
    from urllib.parse import urlsplit

    item_rx = {"display": re.compile(r"display-(homes?|villages?)"), "package": re.compile(r"house-?(and|&)?-?land|packages"),
               "design": re.compile(r"(home|house)-designs|our-homes|/designs|floor-?plans")}
    per_type: dict[str, int] = {}
    existing_ct = {(s.url.rstrip("/"), s.context_key) for s in db.scalars(select(Source).where(Source.brand_id == brand.id))}
    for l in links:
        href = l["href"].split("#")[0].split("?")[0]
        path = urlsplit(href).path
        if not content_mod.same_site(href, list(brand.official_domains or [])) or path.count("/") > 3:
            continue
        for itype, rx in item_rx.items():
            if rx.search(path.lower()) and per_type.get(itype, 0) < 2 and (href.rstrip("/"), itype) not in existing_ct:
                db.add(Source(brand_id=brand.id, kind="competitor_page", url=href, context_key=itype, context={"item_type": itype}, state=source.state,
                              discovered_from_id=source.id, notes=f"discovered from {source.url}"))
                existing_ct.add((href.rstrip("/"), itype))
                per_type[itype] = per_type.get(itype, 0) + 1
                added += 1
                break
    return added


def _latest_ok_snapshot(db, source_id: int):
    return db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == source_id, SourceSnapshot.outcome == "ok").order_by(SourceSnapshot.id.desc()).limit(1))


@job("extract_offers")
def extract_offers(ctx: JobContext) -> dict:
    """Turn new successful snapshots of official offer pages into verified offer versions; follow linked detail pages and terms."""
    out = {"processed": 0, "offers": 0, "discovered_sources": 0, "terms_enriched": 0, "errors": []}
    with session_scope() as db:
        llm = llm_mod.LLM(db, ctx.run_id) if llm_mod.available() else None
        out["method"] = "llm" if llm else "rules"
        for round_no in range(3):  # round 1 listings, rounds 2-3 pick up detail pages / terms they link to
            ids = [r[0] for r in db.execute(select(Source.id).where(Source.active.is_(True), Source.kind.in_(["discovery_seed", "offer_listing", "offer_detail", "serp_offers"])).order_by(Source.id))]
            newly_fetched = 0
            for sid in ids:
                src = db.get(Source, sid)
                snap = _latest_ok_snapshot(db, sid)
                if snap is None:
                    if round_no > 0 and src.discovered_from_id and src.last_attempt_at is None and newly_fetched < 40:
                        snap, _ = fetcher.fetch_source(db, src)  # a source discovered this run: fetch it inline
                        newly_fetched += 1
                        db.commit()
                        if snap.outcome != "ok":
                            continue
                    else:
                        continue
                if snap.extraction_notes is not None:
                    continue
                try:
                    if src.kind == "discovery_seed":
                        out["discovered_sources"] += discover_from_seed(db, src, snap)
                        snap.extraction_notes = {"extracted": False, "discovery": True}
                    else:
                        r = pipeline.process_source(db, src, snap, llm)
                        out["processed"] += 1
                        out["offers"] += r.get("offers", 0)
                        if r.get("error"):
                            out["errors"].append({"source": src.url, "error": r["error"]})
                    db.commit()
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    log.exception("extract failed for %s", src.url)
                    out["errors"].append({"source": src.url, "error": f"{type(e).__name__}: {e}"})
            # terms / pdf sources: fetch if missing, then enrich
            tids = [r[0] for r in db.execute(select(Source.id).where(Source.active.is_(True), Source.kind.in_(["terms", "pdf"]), Source.discovered_from_id.is_not(None)))]
            for tid in tids:
                tsrc = db.get(Source, tid)
                tsnap = _latest_ok_snapshot(db, tid)
                if tsnap is None and tsrc.last_attempt_at is None and newly_fetched < 60:
                    tsnap, _ = fetcher.fetch_source(db, tsrc)
                    newly_fetched += 1
                    db.commit()
                    if tsnap.outcome != "ok":
                        tsnap = None
                if tsnap is None:
                    continue
                offers = db.scalars(select(Offer).join(pipeline.OfferSource, pipeline.OfferSource.offer_id == Offer.id)
                                    .where(Offer.terms_url == tsrc.url)).all()
                for o in offers:
                    try:
                        if pipeline.enrich_with_terms(db, o, tsrc, tsnap, llm):
                            out["terms_enriched"] += 1
                        elif llm is None:
                            pipeline._link_terms(db, o, tsrc, tsnap, now_utc())
                        db.commit()
                    except Exception as e:  # noqa: BLE001
                        db.rollback()
                        out["errors"].append({"source": tsrc.url, "error": f"{type(e).__name__}: {e}"})
            if newly_fetched == 0:
                break
        out["status_changes"] = pipeline.refresh_all_statuses(db)
    return out


@job("detect_events")
def detect_events(ctx: JobContext) -> dict:
    """Competitor design/package/display tracking plus events derived from material offer changes."""
    from datetime import timedelta

    out = {"pages": 0, "new_items": 0, "price_changes": 0, "offer_events": 0, "errors": []}
    with session_scope() as db:
        ids = [r[0] for r in db.execute(select(Source.id).where(Source.active.is_(True), Source.kind == "competitor_page"))]
        fetched = 0
        for sid in ids:
            src = db.get(Source, sid)
            snap = _latest_ok_snapshot(db, sid)
            if snap is None and src.last_attempt_at is None and fetched < 80:  # discovered today: collect it now so the baseline exists immediately
                snap, _ = fetcher.fetch_source(db, src)
                fetched += 1
                db.commit()
                if snap.outcome != "ok":
                    continue
            if snap is None or (snap.extraction_notes or {}).get("events_done"):
                continue
            try:
                r = events_mod.process_competitor_page(db, src, snap)
                snap.extraction_notes = {**(snap.extraction_notes or {}), "events_done": True}
                out["pages"] += 1
                out["new_items"] += r["new"]
                out["price_changes"] += r["price_changes"]
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                out["errors"].append({"source": src.url, "error": f"{type(e).__name__}: {e}"})
        since = now_utc() - timedelta(days=int(ctx.params.get("lookback_days", 2)))
        out["offer_events"] = events_mod.events_from_offers(db, since)
    return out


@job("collect_search")
def collect_search(ctx: JobContext) -> dict:
    """Daily Google results (organic for offers, News for activity) per brand via DataForSEO. Stored as snapshots; reading them is a separate step."""
    from .services.serp import collect as C
    from .services.serp.provider import DataForSEOSerp
    from .services.trends.provider import ProviderError

    try:
        client = DataForSEOSerp()
    except ProviderError as e:
        return {"_status": "skipped", "not_connected": str(e)}
    with session_scope() as db:
        out = C.collect(db, client, ctx.run_id, kinds=tuple(ctx.params.get("kinds") or C.SERP_KINDS), limit=ctx.params.get("limit"), brand_slug=ctx.params.get("brand"), source_ids=ctx.params.get("source_ids"))
    if out["errors"]:
        out["errors"] = out["errors"][:20]
    return out


@job("analyse_activity")
def analyse_activity(ctx: JobContext) -> dict:
    """Turn new Google News results into competitor events (AI-judged when connected, plain headlines otherwise)."""
    from .services.serp.activity import process_news_source

    out = {"sources": 0, "new_events": 0, "judged": 0, "errors": []}
    with session_scope() as db:
        llm = llm_mod.LLM(db, ctx.run_id) if llm_mod.available() else None
        out["method"] = "ai" if llm else "headlines"
        for sid in [r[0] for r in db.execute(select(Source.id).where(Source.active.is_(True), Source.kind == "serp_news").order_by(Source.id))]:
            src = db.get(Source, sid)
            snap = _latest_ok_snapshot(db, sid)
            if snap is None or (snap.extraction_notes or {}).get("events_done"):
                continue
            try:
                r = process_news_source(db, src, snap, llm)
                snap.extraction_notes = {"events_done": True, **r}
                out["sources"] += 1
                out["new_events"] += r["new_events"]
                out["judged"] += r["judged"]
                if r.get("ai_error"):
                    out["errors"].append({"source": src.url, "error": r["ai_error"]})
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                out["errors"].append({"source": src.url, "error": f"{type(e).__name__}: {e}"[:200]})
    return out


@job("check_macro")
def check_macro(ctx: JobContext) -> dict:
    """Daily release check for ABS building approvals, ABS house-construction output PPI and the RBA cash rate."""
    from .services import macro

    with session_scope() as db:
        out = macro.check_all(db)
    return {"datasets": out["datasets"], "errors": out["errors"]}


@job("collect_news")
def collect_news(ctx: JobContext) -> dict:
    """Daily news discovery for configured brand/theme/state queries (Google News RSS)."""
    from .services import news

    with session_scope() as db:
        return news.collect(db)


def _trends_client():
    from .services.trends.provider import DataForSEOTrends, ProviderError

    try:
        return DataForSEOTrends(), None
    except ProviderError as e:
        return None, str(e)


@job("trends_capability")
def trends_capability(ctx: JobContext) -> dict:
    """Real provider test: sync the AU location catalogue and test national + each state (and optionally cities)."""
    from .services.trends import service as T

    client, why = _trends_client()
    if client is None:
        return {"_status": "skipped", "not_connected": why}
    with session_scope() as db:
        added = T.sync_locations(db, client)
        geos = ctx.params.get("geos") or T.GEO_KEYS + ([f"city:{c}" for c in T.CITY_NAMES.values()] if ctx.params.get("cities") else [])
        res = T.capability_test(db, client, geos, ctx.run_id)
        return {"locations_added": added, "tested": res}


@job("refresh_trends")
def refresh_trends(ctx: JobContext) -> dict:
    """Weekly: refresh active comparison plans (brand groups of <=5 and theme keyword groups). Cached, bounded and cost-limited."""
    from .services.trends import service as T

    client, why = _trends_client()
    if client is None:
        return {"_status": "skipped", "not_connected": why}
    with session_scope() as db:
        if not T.resolve_location(db, "AU"):
            T.sync_locations(db, client)
        created = T.build_default_plans(db)
        db.commit()
        out = T.refresh_plans(db, client, ctx.run_id, only_pilot=bool(ctx.params.get("pilot_only")), limit=ctx.params.get("limit"))
        out["plans_created"] = created
        if out["failed"]:
            out["errors"] = out["failed"]
        return out


@job("discover_related")
def discover_related(ctx: JobContext) -> dict:
    """Weekly related/rising query discovery: one seed keyword per request, filtered for home-building relevance, proposed for review."""
    from .services.trends import service as T
    from .services import spend
    from .services.trends.provider import ProviderError

    client, why = _trends_client()
    if client is None:
        return {"_status": "skipped", "not_connected": why}
    seeds = ctx.params.get("seeds") or ["home builders", "house and land packages", "display homes", "knockdown rebuild", "dual occupancy"]
    out = {"requests": 0, "proposed": 0, "errors": []}
    with session_scope() as db:
        for seed in seeds[: int(ctx.params.get("limit", 5))]:
            try:
                T.fetch_compare(db, client, [seed], "AU", days=365, purpose="discover", job_run_id=ctx.run_id, kind="discover", requests_so_far=out["requests"])
                out["requests"] += 1
                db.commit()
            except spend.BudgetExceeded as e:
                out["budget_message"] = str(e)
                break
            except ProviderError as e:
                db.commit()
                out["errors"].append({"seed": seed, "error": str(e)[:160]})
        from .models import RelatedQueryCandidate
        out["proposed"] = db.scalar(select(func.count()).select_from(RelatedQueryCandidate).where(RelatedQueryCandidate.review_status == "proposed")) or 0
    return out


@job("generate_briefs")
def generate_briefs(ctx: JobContext) -> dict:
    """Weekly national + state briefs, after the collectors. Evidence is already persisted; AI failure leaves previous briefs untouched."""
    from .services import briefs as B
    from .timeutil import monday_of, today_local

    week = monday_of(today_local())
    out = {"week_start": week.isoformat(), "briefs": {}, "errors": []}
    if not llm_mod.available():
        return {"_status": "skipped", "not_connected": llm_mod.status().get("reason"), "week_start": week.isoformat()}
    with session_scope() as db:
        llm = llm_mod.LLM(db, ctx.run_id)
        for scope in B.SCOPES:
            try:
                b = B.generate_brief(db, scope, week, llm, ctx.run_id)
                db.commit()
                out["briefs"][scope] = {"status": b.status, "highlights": len(b.highlights), "error": b.error}
                if b.status != "ok":
                    out["errors"].append({"scope": scope, "error": b.error})
            except Exception as e:  # noqa: BLE001
                db.rollback()
                out["errors"].append({"scope": scope, "error": f"{type(e).__name__}: {e}"[:200]})
    return out


@job("import_gsc")
def import_gsc(ctx: JobContext) -> dict:
    """Import an uploaded GSC workbook (idempotent), map themes with rules, and propose representative terms."""
    import os
    from datetime import date as _date

    from .services import gsc

    p = ctx.params
    path = p["path"]
    try:
        with session_scope() as db:
            r = gsc.import_workbook(db, path, file_name=p.get("file_name"), user_id=p.get("user_id"),
                                    period_start=_date.fromisoformat(p["period_start"]) if p.get("period_start") else None,
                                    period_end=_date.fromisoformat(p["period_end"]) if p.get("period_end") else None, period_note=p.get("period_note"))
            if r["created"]:
                r["terms"] = gsc.propose_representative_terms(db, r["import_id"])
        return r
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@job("classify_gsc")
def classify_gsc(ctx: JobContext) -> dict:
    """Batch-classify ambiguous high-impression queries with Claude (optional; rules already cover the bulk)."""
    from .models import GscImport
    from .services import gsc

    if not llm_mod.available():
        return {"_status": "skipped", "not_connected": llm_mod.status()["reason"]}
    with session_scope() as db:
        imp = db.scalar(select(GscImport).order_by(GscImport.id.desc()).limit(1))
        if not imp:
            return {"_status": "skipped", "reason": "no GSC import yet"}
        return gsc.classify_unmapped(db, llm_mod.LLM(db, ctx.run_id), imp.id, limit=int(ctx.params.get("limit", 300)))
