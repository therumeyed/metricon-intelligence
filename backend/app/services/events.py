"""Competitor activity events (brief s7).

Rules:
* A source's first capture is a baseline: items are recorded, no 'new' events are raised.
* Items are matched by a stable identity (their page path). A price change is only reported when the identity is the
  same on both sides, so a different lot/floorplan is never reported as a price change to the same product.
* Observation dates are when *we* first saw something. They are never labelled launch dates. effective_date stays empty
  unless the source states one.
* Offer versions with a real field-level diff become 'campaign_change' events; formatting-only changes never reach here
  because versions are only created when material fields change.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Brand, CompetitorEvent, Offer, Source, SourceSnapshot, TrackedItem
from ..timeutil import now_utc
from . import fetcher
from .offers import pipeline
from .offers.verify import money_values

ITEM_PATTERNS = {
    "display": re.compile(r"/(display-homes?|display-villages?|display-centres?)/[^/]+", re.I),
    "design": re.compile(r"/(home-designs?|house-designs?|designs?|our-homes|homes|floorplans?|ranges?|collections?)/[^/]+(/[^/]+)?", re.I),
    "package": re.compile(r"/(house-and-land|house-land|packages?|h-l|land-and-home)/[^/]+(/[^/]+)?", re.I),
    "estate": re.compile(r"/(estates?|communities|land-for-sale)/[^/]+", re.I),
}
THEME_FOR = {"display": "display-homes", "design": "designs-floorplans", "package": "house-and-land", "estate": "house-and-land"}
MISSING_AFTER = 3


def parse_items(source: Source, links: list[dict], item_type: str) -> list[dict]:
    rx = ITEM_PATTERNS.get(item_type)
    if rx is None:
        return []

    items: dict[str, dict] = {}
    for l in links:
        href = l["href"].split("#")[0]
        p = urlsplit(href)
        if not p.path or p.path.rstrip("/") == urlsplit(source.url).path.rstrip("/"):
            continue
        if not rx.search(p.path) or p.path.lower().endswith((".jpg", ".png", ".pdf", ".svg")):
            continue
        key = p.path.rstrip("/").lower()
        text = (l["text"] or "").strip()
        name = text if len(text) >= 3 else key.rsplit("/", 1)[-1].replace("-", " ").title()
        prices = money_values(text)
        it = items.setdefault(key, {"key": key, "url": href.split("?")[0], "name": name[:160], "price": None})
        if len(name) > len(it["name"]) and len(name) <= 160:
            it["name"] = name
        if prices and it["price"] is None:
            it["price"] = min(prices) if len(prices) else None
    return list(items.values())[:400]


def emit(db: Session, *, key: str, brand: Brand, event_type: str, title: str, source: Source | None, summary: str | None = None, state: str | None = None,
         theme: str | None = None, before=None, after=None, snap_before=None, snap_after=None, evidence=None, offer: Offer | None = None, now: datetime | None = None,
         effective_date=None, geography=None) -> CompetitorEvent | None:
    if db.scalar(select(CompetitorEvent.id).where(CompetitorEvent.event_key == key)):
        return None  # idempotent: a retried job never duplicates an event
    ev = CompetitorEvent(event_key=key, brand_id=brand.id, event_type=event_type, state=state or (source.state if source else None),
                         geography=geography or {}, theme_slug=theme, title=title, summary=summary, observed_at=now or now_utc(),
                         effective_date=effective_date, source_id=source.id if source else None, source_url=source.url if source else None,
                         before=before, after=after, snapshot_before_id=snap_before, snapshot_after_id=snap_after, evidence=evidence or [],
                         offer_id=offer.id if offer else None)
    db.add(ev)
    db.flush()
    return ev


def process_competitor_page(db: Session, source: Source, snap: SourceSnapshot, now: datetime | None = None) -> dict:
    now = now or now_utc()
    brand = db.get(Brand, source.brand_id)
    item_type = (source.context or {}).get("item_type") or "design"
    links = fetcher.effective_links(db, snap)
    items = parse_items(source, links, item_type)
    existing = {t.item_key: t for t in db.scalars(select(TrackedItem).where(TrackedItem.source_id == source.id))}
    baseline = not existing
    new = changed = 0
    seen = set()
    for it in items:
        seen.add(it["key"])
        price = Decimal(str(it["price"])) if it["price"] is not None else None
        t = existing.get(it["key"])
        if t is None:
            t = TrackedItem(brand_id=brand.id, source_id=source.id, item_type=item_type, item_key=it["key"], name=it["name"], state=source.state,
                            url=it["url"], price=price, first_seen_at=now, first_seen_snapshot_id=snap.id, last_seen_at=now, is_baseline=baseline)
            db.add(t)
            if not baseline:
                emit(db, key=f"item:{source.id}:{it['key']}:new", brand=brand, event_type=f"new_{item_type}", source=source, now=now,
                     title=f"New {item_type.replace('_', ' ')} observed: {it['name']}", theme=THEME_FOR.get(item_type), snap_after=snap.id,
                     summary="First observed on the official page by our crawler. This is not a confirmed launch date.",
                     after={"name": it["name"], "url": it["url"], "price": it["price"]}, evidence=[{"snapshot_id": snap.id, "url": it["url"], "text": it["name"]}])
                new += 1
        else:
            if t.missing_since:
                t.missing_since = None
            if price is not None and t.price is not None and price != t.price and item_type in ("design", "package"):
                emit(db, key=f"item:{source.id}:{it['key']}:price:{t.price}->{price}", brand=brand, event_type="price_change", source=source, now=now,
                     title=f"Advertised price changed for {t.name}", theme=THEME_FOR.get(item_type), snap_before=t.first_seen_snapshot_id, snap_after=snap.id,
                     summary="Same page identity on both sides. Inclusions were not compared, so treat as an advertised-price change only.",
                     before={"price": float(t.price)}, after={"price": float(price)}, evidence=[{"snapshot_id": snap.id, "url": it["url"], "text": it["name"]}])
                changed += 1
            if price is not None:
                t.price = price
            t.last_seen_at = now
    if not baseline:
        missing_cut = 0
        for k, t in existing.items():
            if k not in seen and not t.missing_since:
                t.attributes = {**(t.attributes or {}), "absent_count": (t.attributes or {}).get("absent_count", 0) + 1}
                if t.attributes["absent_count"] >= MISSING_AFTER:
                    t.missing_since = now
                    missing_cut += 1
            elif k in seen and t.attributes:
                t.attributes = {**t.attributes, "absent_count": 0}
    db.flush()
    return {"items": len(items), "baseline": baseline, "new": new, "price_changes": changed}


def events_from_offers(db: Session, since: datetime) -> int:
    """Material offer changes since the last run become events (campaign messaging, strengthened/extended/removed)."""
    n = 0
    for o in db.scalars(select(Offer)):
        brand = db.get(Brand, o.brand_id)
        src = db.get(Source, o.sources[0].source_id) if o.sources else None
        for v in o.versions:
            if v.observed_at < since:
                continue
            if v.version_no == 1:
                if o.is_baseline:
                    continue  # first capture of a source: baseline, not news
                if emit(db, key=f"offer:{o.campaign_id}:v1", brand=brand, event_type="new_offer", source=src, now=v.observed_at, offer=o,
                        title=f"New offer observed: {o.title}", theme="costs-affordability", snap_after=(v.snapshot_ids or [None])[-1],
                        summary="First observed on an official page; the launch date is not known.", after={"headline": o.title},
                        evidence=v.evidence[:4], state=src.state if src else None):
                    n += 1
            elif v.diff:
                labels = ", ".join(sorted({d["field"].split("[")[0].split(".")[0] for d in v.diff}))
                if emit(db, key=f"offer:{o.campaign_id}:v{v.version_no}", brand=brand, event_type="campaign_change", source=src, now=v.observed_at, offer=o,
                        title=f"Offer updated: {o.title} ({labels})", theme="costs-affordability", snap_after=(v.snapshot_ids or [None])[-1],
                        before={d["field"]: d["before"] for d in v.diff}, after={d["field"]: d["after"] for d in v.diff}, evidence=v.evidence[:4],
                        summary="Field-level differences between consecutive extracted versions.", state=src.state if src else None):
                    n += 1
        if o.status == "no_longer_observed" and o.versions and not pipeline.superseded_by_ai(db, o):
            day = (o.last_observed_at or now_utc()).date().isoformat()
            if emit(db, key=f"offer:{o.campaign_id}:gone:{day}", brand=brand, event_type="offer_no_longer_observed", source=src, offer=o,
                    title=f"No longer observed: {o.title}", theme="costs-affordability", state=src.state if src else None,
                    summary="A successful crawl no longer shows this campaign. Not confirmed as withdrawn."):
                n += 1
    return n
