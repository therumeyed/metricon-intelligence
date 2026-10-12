"""Offer capture -> versions -> status. Application code owns identity, dedupe, dates and status; the model only reads text.

Key behaviours (brief s6 / s15):
* First capture of a source is a *baseline*. 'First observed' is never 'launched'.
* A failed fetch keeps the last known offer and marks it stale. It cannot withdraw anything.
* A successful crawl that no longer shows a campaign sets 'no_longer_observed'. 'withdrawn' needs a quoted statement or a human.
* An old PDF/terms page is evidence, not proof of a current advert.
* Template-only page changes leave the promo-window hash unchanged, so nothing new is extracted or versioned.
* Human corrections are stored separately and re-applied over every machine extraction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from copy import deepcopy
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import (
    AuditLog,
    Brand,
    Offer,
    OfferApplicability,
    OfferCorrection,
    OfferMechanism,
    OfferSource,
    OfferVersion,
    Source,
    SourceSnapshot,
)
from ...timeutil import default_tz_for_state, is_expired, now_utc, today_local
from .. import content as content_mod
from .. import fetcher
from ..llm import LLM, LLMError
from ..spend import BudgetExceeded
from . import llm_extract, rules_extract
from .schema import ExtractedOffer, PageExtraction
from .verify import verify_page

log = logging.getLogger(__name__)
EXTRACTION_VERSION = "offers-1.0"
CORRECTABLE = {"title", "headline", "benefits", "geography", "eligibility", "published_start", "published_expiry",
               "expiry_timezone", "expiry_text", "terms_url", "status"}
STATUSES = {"advertised", "expired", "withdrawn", "no_longer_observed", "needs_review"}


# --------------------------------------------------------------------------- identity helpers
def campaign_key(title: str) -> str:
    """Stable identity from the title with numbers/currency removed, so '$50,000 OFF' -> '$60,000 OFF' is the same campaign."""
    t = re.sub(r"[\d$,.%*^#+\-–—:!?'’\"()/&]+", " ", title.lower())
    t = re.sub(r"\b(the|a|an|and|of|to|on|in|with|for|your|our|new|now|get|up|from|only|just|selected)\b", " ", t)
    return re.sub(r"\s+", "-", t.strip())[:180] or "offer"


def new_campaign_id() -> str:
    return "OFR-" + secrets.token_hex(4).upper()


def signature(ex: dict) -> tuple:
    """What must match for two pages to be the same campaign: mechanism types, amounts and expiry."""
    ben = sorted((b["mechanism_type"], b.get("amount"), b.get("amount_qualifier")) for b in ex.get("benefits", []))
    return (tuple(ben), (ex.get("dates") or {}).get("expiry_date"))


def material_fields(ex: dict) -> dict:
    """The facts whose change makes a new version. Evidence quotes and wording of quotes are excluded."""
    out = {
        "title": ex.get("title"), "headline": ex.get("headline"),
        "benefits": [{k: b.get(k) for k in ("mechanism_type", "label", "amount", "amount_qualifier", "amount_kind", "customer_payment_required",
                                           "builder_contribution", "government_component", "tax_basis", "time_promise_type", "time_promise_days")}
                     for b in ex.get("benefits", [])],
        "geography": [{k: g.get(k) for k in ("level", "state", "name")} for g in ex.get("geography", [])],
        "eligibility": {k: (ex.get("eligibility") or {}).get(k) for k in ("ranges", "designs", "contract_condition", "buyer_types", "land_status",
                                                                         "finance_requirements", "exclusions")},
        "dates": {k: (ex.get("dates") or {}).get(k) for k in ("start_date", "expiry_date", "expiry_text", "expiry_timezone")},
        "terms_url": ex.get("terms_url"), "withdrawn": bool(ex.get("withdrawn_explicit")),
    }
    te = ex.get("terms_enrichment")
    if te:
        out["terms_enrichment"] = {"eligibility": {k: (te.get("eligibility") or {}).get(k) for k in ("ranges", "designs", "contract_condition", "buyer_types", "land_status", "finance_requirements", "exclusions")},
                                   "dates": {k: (te.get("dates") or {}).get(k) for k in ("start_date", "expiry_date", "expiry_text")}}
    return out


def fields_hash(material: dict) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def _flat(material: dict) -> dict[str, Any]:
    flat: dict[str, Any] = {"title": material.get("title"), "headline": material.get("headline"), "terms_url": material.get("terms_url")}
    for i, b in enumerate(material.get("benefits", [])):
        flat[f"benefit[{b.get('mechanism_type')}:{i}]"] = {k: v for k, v in b.items() if v not in (None, False)}
    for k, v in (material.get("dates") or {}).items():
        flat[f"dates.{k}"] = v
    for k, v in (material.get("eligibility") or {}).items():
        flat[f"eligibility.{k}"] = v
    flat["geography"] = sorted(f"{g.get('state') or ''}:{g.get('name') or ''}:{g.get('level')}" for g in material.get("geography", []))
    flat["withdrawn"] = material.get("withdrawn")
    for k, v in ((material.get("terms_enrichment") or {}).get("eligibility") or {}).items():
        flat[f"terms.eligibility.{k}"] = v
    return flat


def diff_fields(before: dict | None, after: dict) -> list[dict]:
    if before is None:
        return []
    a, b = _flat(before), _flat(after)
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k) and not (a.get(k) in (None, [], "") and b.get(k) in (None, [], "")):
            out.append({"field": k, "before": a.get(k), "after": b.get(k)})
    return out


# --------------------------------------------------------------------------- geography
def source_geo(source: Source) -> list[dict]:
    """Regional context carried by a page. It is recorded as basis='page_context', never as a stated applicability."""
    ctx = source.context or {}
    if source.state and ctx.get("scope_level") and ctx.get("area"):
        return [{"level": ctx["scope_level"], "state": source.state, "name": ctx["area"], "as_stated": f"Listed on {ctx['area']} offers page", "basis": "page_context"}]
    return []


def _geo_key(g: dict) -> tuple:
    return (g.get("state") or "", (g.get("name") or "").lower(), g.get("level"))


def merge_geo(stated: list[dict], contexts: list[list[dict]]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for g in stated:
        seen[_geo_key(g)] = {**g, "basis": "stated"}
    for lst in contexts:
        for g in lst:
            seen.setdefault(_geo_key(g), g)
    return list(seen.values())


# --------------------------------------------------------------------------- effective fields + corrections
def active_corrections(db: Session, offer: Offer) -> dict[str, OfferCorrection]:
    rows = db.scalars(select(OfferCorrection).where(OfferCorrection.offer_id == offer.id, OfferCorrection.superseded_at.is_(None))
                      .order_by(OfferCorrection.id)).all()
    return {r.field: r for r in rows}


def build_effective(db: Session, offer: Offer) -> dict:
    """Machine fields (latest version) + page-context geography + human corrections."""
    v = offer.versions[-1] if offer.versions else None
    f = deepcopy(v.fields) if v else {"title": offer.title, "benefits": [], "geography": [], "eligibility": {}, "dates": {}}
    te = f.get("terms_enrichment") or {}
    elig = f.get("eligibility") or {}
    tel = te.get("eligibility") or {}
    merged_elig = {k: elig.get(k) for k in ("ranges", "designs", "contract_condition", "buyer_types", "land_status", "finance_requirements", "exclusions")}
    for k in ("ranges", "designs", "buyer_types", "finance_requirements", "exclusions"):
        merged_elig[k] = list(dict.fromkeys((elig.get(k) or []) + (tel.get(k) or [])))
    for k in ("contract_condition", "land_status"):
        merged_elig[k] = elig.get(k) or tel.get(k)
    dates = dict(f.get("dates") or {})
    tdates = te.get("dates") or {}
    for k in ("start_date", "expiry_date", "expiry_text"):
        if not dates.get(k) and tdates.get(k):
            dates[k] = tdates[k]
    ctx_geos = []
    for os_ in offer.sources:
        if os_.role in ("listing", "detail") and os_.geo:
            ctx_geos.append(os_.geo)
    eff = {
        "title": f.get("title") or offer.title, "headline": f.get("headline"),
        "benefits": f.get("benefits") or [],
        "geography": merge_geo(f.get("geography") or [], ctx_geos),
        "eligibility": merged_elig,
        "published_start": dates.get("start_date"), "published_expiry": dates.get("expiry_date"),
        "expiry_text": dates.get("expiry_text"), "expiry_timezone": dates.get("expiry_timezone"),
        "year_inferred": dates.get("year_inferred", False),
        "terms_url": f.get("terms_url") or offer.terms_url, "detail_url": f.get("detail_url"),
        "withdrawn_explicit": bool(f.get("withdrawn_explicit")), "withdrawn_evidence": f.get("withdrawn_evidence"),
        "conflicts": list(dict.fromkeys((f.get("conflicts") or []) + (te.get("conflicts") or []))),
        "unresolved": list(dict.fromkeys((v.unresolved if v else []) + (te.get("still_unresolved") or []))),
        "corrected_fields": [],
    }
    for field, corr in active_corrections(db, offer).items():
        if field in CORRECTABLE and field != "status":
            eff[field] = corr.value
            eff["corrected_fields"].append(field)
    return eff


def rebuild_derived(db: Session, offer: Offer, now: datetime | None = None) -> None:
    now = now or now_utc()
    eff = build_effective(db, offer)
    offer.effective = eff
    offer.title = eff["title"] or offer.title
    offer.headline = eff.get("headline")
    offer.terms_url = eff.get("terms_url")
    offer.expiry_text = eff.get("expiry_text")
    offer.published_start = date.fromisoformat(eff["published_start"]) if eff.get("published_start") else None
    offer.published_expiry = date.fromisoformat(eff["published_expiry"]) if eff.get("published_expiry") else None
    # stated tz wins; otherwise the state's local zone is *assumed* and flagged
    states = {g.get("state") for g in eff["geography"] if g.get("state")}
    tz = eff.get("expiry_timezone")
    offer.expiry_timezone = tz or (default_tz_for_state(next(iter(states))) if len(states) == 1 else None)
    eff["expiry_timezone_assumed"] = bool(offer.published_expiry and not tz)
    eff["expiry_timezone"] = offer.expiry_timezone
    offer.conflicts = eff["conflicts"]
    # derived rows
    for m in list(offer.mechanisms):
        db.delete(m)
    for a in list(offer.applicability):
        db.delete(a)
    db.flush()
    offer.mechanisms, offer.applicability = [], []
    for b in eff["benefits"]:
        offer.mechanisms.append(OfferMechanism(
            mechanism_type=b["mechanism_type"], label=b.get("label") or b["mechanism_type"], amount=b.get("amount"),
            amount_qualifier=b.get("amount_qualifier"), amount_kind=b.get("amount_kind"),
            customer_payment_required=b.get("customer_payment_required"), builder_contribution=b.get("builder_contribution"),
            government_component=bool(b.get("government_component")), tax_basis=b.get("tax_basis"),
            time_promise_type=b.get("time_promise_type"), time_promise_days=b.get("time_promise_days"), excerpt=(b.get("evidence") or "")[:400]))
    for g in eff["geography"]:
        offer.applicability.append(OfferApplicability(scope_level=g.get("level") or "state", state=g.get("state"), name=g.get("name"),
                                                      as_stated=f"{g.get('as_stated') or g.get('name') or ''} [{g.get('basis', 'stated')}]"))
    refresh_status(db, offer, now)
    refresh_flags(db, offer)


def refresh_flags(db: Session, offer: Offer) -> None:
    eff = offer.effective or {}
    v = offer.versions[-1] if offer.versions else None
    reasons = []
    if v and v.extraction_method == "rules":
        reasons.append("Extracted by rules only (no AI model): conditions and eligibility need a human check")
    if eff.get("conflicts"):
        reasons.append("Landing page and terms conflict - definitive comparison needs review")
    if eff.get("year_inferred"):
        reasons.append("Expiry year was not printed; inferred from the observation date")
    if "~" in offer.campaign_key:
        reasons.append("Same title as another campaign but different conditions, amounts or expiry - kept separate pending review")
    if offer.status == "needs_review":
        reasons.append(offer.status_reason or "Status cannot be established from current evidence")
    for c in active_corrections(db, offer).values():
        pass
    offer.review_reasons = reasons
    offer.needs_review = bool(reasons)


# --------------------------------------------------------------------------- status
def _latest_ok(db: Session, source_id: int) -> SourceSnapshot | None:
    return db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == source_id, SourceSnapshot.outcome == "ok")
                     .order_by(SourceSnapshot.id.desc()).limit(1))


def refresh_status(db: Session, offer: Offer, now: datetime | None = None) -> None:
    now = now or now_utc()
    if offer.status_set_by_human:
        return
    eff = offer.effective or {}
    page_sources = [s for s in offer.sources if s.role in ("listing", "detail")]
    if eff.get("withdrawn_explicit"):
        offer.status, offer.status_reason = "withdrawn", f"Source states the offer is withdrawn: \"{(eff.get('withdrawn_evidence') or '')[:200]}\""
        return
    if offer.published_expiry and is_expired(offer.published_expiry, offer.expiry_timezone, now):
        offer.status, offer.status_reason = "expired", f"Published expiry {offer.published_expiry.isoformat()} has passed"
        return
    if not page_sources:
        offer.status, offer.status_reason = "needs_review", "Seen only in terms/PDF evidence, not on a current official offer page"
        return
    seen_now, absent_after_crawl, unknown = 0, 0, 0
    for os_ in page_sources:
        latest = _latest_ok(db, os_.source_id)
        if latest is None:
            unknown += 1
        elif os_.last_seen_snapshot_id == latest.id:
            seen_now += 1
        elif latest.id > (os_.last_seen_snapshot_id or 0):
            absent_after_crawl += 1
        else:
            unknown += 1
    if seen_now:
        offer.status, offer.status_reason = "advertised", "Shown on a current official page at last successful check"
    elif absent_after_crawl and not unknown and absent_after_crawl == len(page_sources):
        offer.status = "no_longer_observed"
        offer.status_reason = "Last successful crawl of every page that listed it no longer shows it (not confirmed withdrawn)"
    # otherwise: leave the previous status untouched (e.g. fetch failures)


def freshness(offer: Offer, now: datetime | None = None) -> dict:
    now = now or now_utc()
    s = get_settings()
    ok = offer.last_success_check_at
    failed_after = bool(offer.last_failed_attempt_at and (not ok or offer.last_failed_attempt_at > ok))
    too_old = bool(ok is None or (now - ok).total_seconds() > s.stale_after_hours * 3600)
    return {"state": "stale" if (failed_after or too_old) else "fresh", "last_success_check_at": ok,
            "last_failed_attempt_at": offer.last_failed_attempt_at, "failure_reason": offer.last_failure_reason if failed_after else None}


# --------------------------------------------------------------------------- matching + upsert
def _link(db: Session, offer: Offer, source: Source, snap: SourceSnapshot, role: str, now: datetime) -> OfferSource:
    os_ = next((x for x in offer.sources if x.source_id == source.id), None)
    if os_ is None:
        os_ = OfferSource(offer_id=offer.id, source_id=source.id, role=role, first_seen_snapshot_id=snap.id)
        offer.sources.append(os_)
    os_.role = role
    os_.last_seen_snapshot_id, os_.last_seen_at = snap.id, now
    os_.geo = source_geo(source)
    return os_


def match_offer(db: Session, brand: Brand, source: Source, ex: dict, key: str, prior_here: list[Offer]) -> tuple[Offer | None, str | None]:
    """-> (existing offer or None, review note). Dedupe only after confirming identity + conditions."""
    for o in prior_here:
        if o.campaign_key == key:
            return o, None
    if len(prior_here) == 1 and not ex.get("_multi"):
        # the single offer previously shown on this page was re-titled: treat as an edit of the same campaign
        return prior_here[0], None
    for o in db.scalars(select(Offer).where(Offer.brand_id == brand.id)):
        if (o.effective or {}).get("detail_url") and ex.get("_source_url") and o.effective["detail_url"].rstrip("/") == ex["_source_url"].rstrip("/"):
            return o, None
    existing = db.scalar(select(Offer).where(Offer.brand_id == brand.id, Offer.campaign_key == key))
    if existing is not None:
        cur = existing.versions[-1].fields if existing.versions else {}
        if signature(cur) == signature(ex):
            return existing, None
        return None, f"Same title as {existing.campaign_id} but different conditions/amounts/expiry - kept as a separate campaign pending review"
    return None, None


def upsert_offer(db: Session, *, brand: Brand, source: Source, snap: SourceSnapshot, ex: ExtractedOffer, method: str, model: str | None,
                 is_baseline: bool, role: str, prior_here: list[Offer], multi: bool, now: datetime, evidence_notes: list[str]) -> Offer:
    d = json.loads(ex.model_dump_json())
    d["_multi"], d["_source_url"] = multi, source.url
    key = campaign_key(ex.title)
    offer, note = match_offer(db, brand, source, d, key, prior_here)
    d.pop("_multi"), d.pop("_source_url")
    created = False
    if offer is None:
        if note:
            key = f"{key}~{hashlib.sha1(json.dumps(signature(d), default=str).encode()).hexdigest()[:6]}"
        offer = Offer(campaign_id=new_campaign_id(), brand_id=brand.id, campaign_key=key, title=ex.title, source_url=source.url,
                      first_observed_at=now, is_baseline=is_baseline, status="advertised")
        db.add(offer)
        db.flush()
        created = True
    if note:
        offer.status_reason = note
    _link(db, offer, source, snap, role, now)
    if role == "listing" and not offer.source_url:
        offer.source_url = source.url
    if role == "detail" or not offer.source_url:
        offer.source_url = source.url
    material = material_fields(d)
    h = fields_hash(material)
    last = offer.versions[-1] if offer.versions else None
    detail_is_primary = role == "listing" and any(x.role == "detail" for x in offer.sources)
    if detail_is_primary and last is not None:
        pass  # the offer's own page is the primary record; a card summary must not flip-flop its versions
    elif last is None or last.fields_hash != h:
        # keep terms enrichment from the previous version when a page re-extraction does not carry it
        if last is not None and "terms_enrichment" in last.fields and "terms_enrichment" not in d:
            d["terms_enrichment"] = last.fields["terms_enrichment"]
            material = material_fields(d)
            h = fields_hash(material)
        if last is None or last.fields_hash != h:
            ev = [{"field": "benefit", "excerpt": b["evidence"], "snapshot_id": snap.id, "source_url": snap.final_url or source.url} for b in d["benefits"]]
            ev += [{"field": "eligibility", "excerpt": q, "snapshot_id": snap.id, "source_url": snap.final_url or source.url} for q in d["eligibility"].get("evidence", [])]
            if d["dates"].get("evidence"):
                ev.append({"field": "dates", "excerpt": d["dates"]["evidence"], "snapshot_id": snap.id, "source_url": snap.final_url or source.url})
            ver = OfferVersion(offer_id=offer.id, version_no=(last.version_no + 1) if last else 1, fields_hash=h, fields=d, evidence=ev,
                               unresolved=list(dict.fromkeys(d.get("unresolved", []) + evidence_notes)),
                               diff=diff_fields(material_fields(last.fields) if last else None, material),
                               snapshot_ids=[snap.id], extraction_method=method, extraction_version=EXTRACTION_VERSION, model=model,
                               observed_at=now, is_baseline=created and is_baseline)
            db.add(ver)
            db.flush()
            offer.versions.append(ver)
            offer.current_version_id = ver.id
    offer.last_observed_at = now
    offer.last_success_check_at = now
    offer.last_failed_attempt_at = offer.last_failed_attempt_at if (offer.last_failed_attempt_at and offer.last_failed_attempt_at > now) else None
    offer.last_failure_reason = None
    if offer.status == "no_longer_observed":
        offer.status_reason = "Re-observed on a current official page"
    db.flush()
    rebuild_derived(db, offer, now)
    return offer


# --------------------------------------------------------------------------- source processing
def _offers_on_source(db: Session, source: Source) -> list[Offer]:
    return list(db.scalars(select(Offer).join(OfferSource, OfferSource.offer_id == Offer.id).where(OfferSource.source_id == source.id)))


def record_failure(db: Session, source: Source, snap: SourceSnapshot, now: datetime | None = None) -> int:
    """A failed fetch keeps every offer as-is and flags it stale. Never changes status."""
    now = now or now_utc()
    n = 0
    for o in _offers_on_source(db, source):
        o.last_failed_attempt_at, o.last_failure_reason = now, f"{snap.outcome}: {snap.error or ''}"[:300]
        n += 1
    return n


def _page_text(blocks: list[str], kind: str) -> str:
    if kind in ("offer_listing", "offer_detail"):
        return "\n".join(content_mod.promo_window(blocks))
    return "\n".join(blocks)[:30000]


def _detail_links(source: Source, links: list[dict], domains: list[str]) -> dict[str, str]:
    base = urlsplit(source.url)
    out: dict[str, str] = {}
    for l in links:
        href = l["href"].split("#")[0]
        u = urlsplit(href)
        if u.hostname and domains and not content_mod.same_site(href, domains):
            continue
        if href.rstrip("/") == source.url.rstrip("/"):
            continue
        parent = base.path.rstrip("/")
        if u.path.startswith(parent + "/") and len(u.path) > len(parent) + 1 and not u.path.lower().endswith((".jpg", ".png", ".svg")):
            out[href] = l["text"]
    return out


def register_linked_sources(db: Session, source: Source, brand: Brand, offers: list[ExtractedOffer], links: list[dict]) -> int:
    """Detail pages and terms/PDF links discovered from a listing become sources (capped)."""
    domains = list(brand.official_domains or [])
    link_urls = {l["href"].split("#")[0] for l in links}
    added = 0
    existing = {(s.url) for s in db.scalars(select(Source).where(Source.brand_id == brand.id))}
    wanted: list[tuple[str, str]] = []
    for o in offers:
        for url, kind in ((o.detail_url, "offer_detail"), (o.terms_url, "terms")):
            if url and url in link_urls:
                k = "pdf" if url.lower().split("?")[0].endswith(".pdf") else kind
                wanted.append((url.split("#")[0], k))
    for url, k in wanted:
        if url in existing or added >= 30:
            continue
        if domains and not content_mod.same_site(url, domains + ["metricon.com.au", "files.metricon.com.au"]):
            continue
        db.add(Source(brand_id=brand.id, kind=k, url=url, context_key=source.context_key, context=source.context, state=source.state,
                      discovered_from_id=source.id, notes="discovered from " + source.url))
        existing.add(url)
        added += 1
    db.flush()
    return added


def process_source(db: Session, source: Source, snap: SourceSnapshot, llm: LLM | None, now: datetime | None = None) -> dict:
    """Extract offers from one successfully fetched listing/detail snapshot."""
    now = now or now_utc()
    brand = source.brand or db.get(Brand, source.brand_id)
    text_full = fetcher.effective_text(db, snap) or ""
    blocks = text_full.split("\n")
    links = fetcher.effective_links(db, snap)
    mat_hash = content_mod.material_hash(blocks)
    prior_snaps = db.scalars(select(SourceSnapshot).where(SourceSnapshot.source_id == source.id, SourceSnapshot.id < snap.id,
                                                          SourceSnapshot.outcome == "ok").order_by(SourceSnapshot.id.desc())).all()
    prior_extracted = next((p for p in prior_snaps if (p.extraction_notes or {}).get("extracted")), None)
    is_baseline = prior_extracted is None
    prior_offers = _offers_on_source(db, source)
    result = {"source": source.url, "extracted": False, "offers": 0, "baseline": is_baseline}

    # --- unchanged promo window => nothing to extract; the offers we know are still shown
    if prior_extracted and (prior_extracted.extraction_notes or {}).get("material_hash") == mat_hash:
        seen_ids = set((prior_extracted.extraction_notes or {}).get("seen_offer_ids", []))
        for o in prior_offers:
            if o.id in seen_ids:
                _link(db, o, source, snap, next((x.role for x in o.sources if x.source_id == source.id), "listing"), now)
                o.last_observed_at = o.last_success_check_at = now
                o.last_failed_attempt_at, o.last_failure_reason = None, None
                rebuild_derived(db, o, now)
        snap.extraction_notes = {"extracted": True, "material_hash": mat_hash, "seen_offer_ids": sorted(seen_ids), "skipped": "promo window unchanged"}
        result.update(extracted=True, skipped_unchanged=True, offers=len(seen_ids))
        return result

    text = _page_text(blocks, source.kind)
    method, model, page, notes = "rules", None, None, []
    if llm is not None:
        try:
            if text.strip():
                page = llm_extract.extract_page(llm, brand=brand.name, url=source.url, text=text, links=links, context=source.context or {}, observed=today_local(now))
            else:
                page = PageExtraction(offers=[], page_has_offers=False)
            method, model = "llm", llm.model
        except (LLMError, BudgetExceeded) as e:
            snap.extraction_notes = {"extracted": False, "material_hash": mat_hash, "error": f"{type(e).__name__}: {e}"[:300]}
            result["error"] = snap.extraction_notes["error"]
            return result  # leave everything as it was; do not fall back to a different extractor mid-history
    if page is None:
        link_map = {l["text"].strip().lower(): l["href"] for l in links if l["text"]}
        page = rules_extract.extract_from_blocks(content_mod.promo_window(blocks), default_year=today_local(now).year, link_map=link_map)
    page, vnotes = verify_page(page, text)
    notes += vnotes
    role = "detail" if source.kind == "offer_detail" else "listing"
    seen: list[Offer] = []
    seen_keys: set[int] = set()
    multi = len(page.offers) > 1
    for ex in page.offers:
        o = upsert_offer(db, brand=brand, source=source, snap=snap, ex=ex, method=method, model=model, is_baseline=is_baseline, role=role,
                         prior_here=prior_offers, multi=multi, now=now, evidence_notes=[n for n in notes if ex.title[:40] in n])
        if o.id not in seen_keys:
            seen.append(o)
            seen_keys.add(o.id)
    register_linked_sources(db, source, brand, page.offers, links)
    snap.extraction_notes = {"extracted": True, "material_hash": mat_hash, "seen_offer_ids": sorted(seen_keys), "method": method,
                             "notes": notes[:40], "page_has_offers": page.page_has_offers}
    # offers previously on this page but not in this extraction: re-evaluate (crawl was successful)
    for o in prior_offers:
        if o.id not in seen_keys:
            rebuild_derived(db, o, now)
    result.update(extracted=True, offers=len(seen), method=method)
    return result


# --------------------------------------------------------------------------- terms enrichment
def enrich_with_terms(db: Session, offer: Offer, terms_source: Source, terms_snap: SourceSnapshot, llm: LLM | None, now: datetime | None = None) -> bool:
    """Second pass: conditions from the linked terms/PDF text, verified against that text. Returns True if a version was added."""
    now = now or now_utc()
    if llm is None or not offer.versions:
        return False
    text = fetcher.effective_text(db, terms_snap) or ""
    if not text.strip():
        return False
    last = offer.versions[-1]
    if (last.fields.get("terms_enrichment") or {}).get("terms_snapshot_hash") == terms_snap.content_hash:
        return False
    eff = build_effective(db, offer)
    summary = "; ".join(f"{b.get('label')} ({b.get('amount_qualifier') or ''} {b.get('amount') or ''})" for b in eff["benefits"])[:600]
    try:
        te = llm_extract.enrich_from_terms(llm, brand=offer.brand.name, offer_title=offer.title, offer_summary=summary, terms_url=terms_source.url,
                                           terms_text=text, observed=today_local(now))
    except (LLMError, BudgetExceeded) as e:
        log.warning("terms enrichment failed for %s: %s", offer.campaign_id, e)
        return False
    from .verify import norm, quote_in_text, date_supported
    cn = norm(text)
    data = json.loads(te.model_dump_json())
    ev_ok = [q for q in data["eligibility"]["evidence"] if quote_in_text(q, cn)]
    for fld in ("ranges", "designs", "buyer_types", "finance_requirements", "exclusions"):
        data["eligibility"][fld] = [i for i in data["eligibility"][fld] if norm(i) in cn or any(norm(i) in norm(q) for q in ev_ok)]
    for fld in ("contract_condition", "land_status"):
        v = data["eligibility"].get(fld)
        if v and not (norm(v) in cn or any(norm(v) in norm(q) for q in ev_ok)):
            data["eligibility"][fld] = None
    data["eligibility"]["evidence"] = ev_ok
    for fld in ("start_date", "expiry_date"):
        iso = data["dates"].get(fld)
        if iso and date_supported(iso, data["dates"].get("evidence"), text)[0] is None:
            data["dates"][fld] = None
    data["terms_snapshot_hash"], data["terms_snapshot_id"], data["terms_url"] = terms_snap.content_hash, terms_snap.id, terms_source.url
    new_fields = deepcopy(last.fields)
    new_fields["terms_enrichment"] = data
    old_expiry = (last.fields.get("dates") or {}).get("expiry_date")
    if data["dates"].get("expiry_date") and old_expiry and data["dates"]["expiry_date"] != old_expiry:
        new_fields.setdefault("conflicts", []).append(f"Landing page expiry {old_expiry} differs from terms expiry {data['dates']['expiry_date']}")
    material = material_fields(new_fields)
    h = fields_hash(material)
    if h == last.fields_hash:
        last.fields = new_fields  # same facts; record that terms were analysed
        return False
    ver = OfferVersion(offer_id=offer.id, version_no=last.version_no + 1, fields_hash=h, fields=new_fields,
                       evidence=list(last.evidence) + [{"field": "terms", "excerpt": q, "snapshot_id": terms_snap.id, "source_url": terms_source.url} for q in ev_ok],
                       unresolved=data.get("still_unresolved", []), diff=diff_fields(material_fields(last.fields), material),
                       snapshot_ids=list(last.snapshot_ids) + [terms_snap.id], extraction_method="llm", extraction_version=EXTRACTION_VERSION,
                       model=llm.model, observed_at=now)
    db.add(ver)
    db.flush()
    offer.versions.append(ver)
    offer.current_version_id = ver.id
    _link_terms(db, offer, terms_source, terms_snap, now)
    rebuild_derived(db, offer, now)
    return True


def _link_terms(db: Session, offer: Offer, terms_source: Source, snap: SourceSnapshot, now: datetime) -> None:
    os_ = next((x for x in offer.sources if x.source_id == terms_source.id), None)
    if os_ is None:
        os_ = OfferSource(offer_id=offer.id, source_id=terms_source.id, role="pdf" if terms_source.kind == "pdf" else "terms", first_seen_snapshot_id=snap.id)
        offer.sources.append(os_)
    os_.last_seen_snapshot_id, os_.last_seen_at = snap.id, now


# --------------------------------------------------------------------------- human corrections
def apply_correction(db: Session, offer: Offer, field: str, value: Any, user_id: int | None, reason: str | None = None) -> OfferCorrection:
    if field not in CORRECTABLE:
        raise ValueError(f"field {field!r} cannot be corrected")
    now = now_utc()
    if field == "status":
        if value not in STATUSES:
            raise ValueError("invalid status")
        before = offer.status
        offer.status, offer.status_set_by_human, offer.status_reason = value, True, f"Set by a person{': ' + reason if reason else ''}"
        corr = OfferCorrection(offer_id=offer.id, field="status", value=value, machine_value=before, reason=reason, user_id=user_id)
        for old in db.scalars(select(OfferCorrection).where(OfferCorrection.offer_id == offer.id, OfferCorrection.field == "status", OfferCorrection.superseded_at.is_(None))):
            old.superseded_at = now
        db.add(corr)
        db.add(AuditLog(user_id=user_id, action="offer.status", entity="offer", entity_id=offer.campaign_id, before=before, after=value, note=reason))
        db.flush()
        refresh_flags(db, offer)
        return corr

    eff_before = (offer.effective or {}).get(field)
    for old in db.scalars(select(OfferCorrection).where(OfferCorrection.offer_id == offer.id, OfferCorrection.field == field, OfferCorrection.superseded_at.is_(None))):
        old.superseded_at = now
    corr = OfferCorrection(offer_id=offer.id, field=field, value=value, machine_value=eff_before, reason=reason, user_id=user_id)
    db.add(corr)
    db.add(AuditLog(user_id=user_id, action="offer.correct", entity="offer", entity_id=offer.campaign_id, before={field: eff_before}, after={field: value}, note=reason))
    db.flush()
    rebuild_derived(db, offer, now)
    return corr


def release_status(db: Session, offer: Offer, user_id: int | None) -> None:
    offer.status_set_by_human = False
    db.add(AuditLog(user_id=user_id, action="offer.status_release", entity="offer", entity_id=offer.campaign_id, note="returned to automatic status"))
    for old in db.scalars(select(OfferCorrection).where(OfferCorrection.offer_id == offer.id, OfferCorrection.field == "status", OfferCorrection.superseded_at.is_(None))):
        old.superseded_at = now_utc()
    rebuild_derived(db, offer)


def refresh_all_statuses(db: Session, now: datetime | None = None) -> int:
    n = 0
    for o in db.scalars(select(Offer)):
        before = o.status
        refresh_status(db, o, now)
        if o.status != before:
            n += 1
        refresh_flags(db, o)
    return n
