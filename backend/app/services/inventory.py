"""Inventory of pages that already exist on Metricon's website, so recommendations never propose something that is already there.

Sources: the GSC page export (pages that earned impressions) and the public sitemap (pages that exist whether or not they rank).
Matching a topic to existing pages is deliberately simple and explainable: topic words against URL path words, with plural folding
and place-name aliases. When in doubt it reports the near matches rather than claiming a gap.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Brand, GscImport, GscPageRow, SitePage
from ..timeutil import now_utc
from . import fetcher
from .gsc import classify_page

MAX_SITEMAPS = 80
MAX_PAGES = 8000
LOC_RX = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
STOP = {"in", "the", "and", "for", "a", "of", "to", "near", "me", "au", "australia", "with", "on", "at", "by", "from", "best", "top"}
GENERIC = {"home", "house", "build", "builder", "new", "design", "plan", "price", "cost", "australia"}
PLACE_ALIASES = {
    "victoria": {"vic", "victoria", "melbourne"}, "vic": {"vic", "victoria", "melbourne"}, "melbourne": {"vic", "victoria", "melbourne"},
    "nsw": {"nsw", "sydney"}, "sydney": {"nsw", "sydney"}, "queensland": {"qld", "queensland", "brisbane"}, "qld": {"qld", "queensland", "brisbane"}, "brisbane": {"qld", "queensland", "brisbane"},
    "sa": {"sa", "adelaide"}, "adelaide": {"sa", "adelaide"},
}


def canonical(url: str, domains: list[str]) -> str | None:
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.scheme not in ("http", "https") or not any(host == d or host.endswith("." + d) for d in domains):
        return None
    if host.startswith("files.") or p.path.lower().endswith((".pdf", ".jpg", ".png", ".svg", ".webp", ".xml", ".css", ".js")):
        return None
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", p.hostname.lower(), path, "", ""))


def own_domains(db: Session) -> list[str]:
    b = db.scalar(select(Brand).where(Brand.is_own.is_(True)))
    return list(b.official_domains or []) if b else []


def _upsert(db: Session, url: str, source: str, now: datetime, gsc: dict | None = None) -> SitePage:
    row = db.scalar(select(SitePage).where(SitePage.url == url))
    if row is None:
        info = classify_page(url)
        row = SitePage(url=url, path=info["path"], sources=[], page_kind=info["page_kind"], theme_slug=info["theme_slug"], state_hint=info["state_hint"], first_seen_at=now, last_seen_at=now)
        db.add(row)
    if source not in row.sources:
        row.sources = [*row.sources, source]
    row.last_seen_at = now
    if gsc is not None:
        row.impressions, row.clicks, row.position = gsc["impressions"], gsc["clicks"], gsc["position"]
    return row


def refresh_from_gsc(db: Session, import_id: int | None = None) -> dict:
    """Pages in the GSC page tab (query strings folded into their canonical page; positions impression-weighted)."""
    now = now_utc()
    imp_id = import_id or db.scalar(select(GscImport.id).order_by(GscImport.id.desc()).limit(1))
    if imp_id is None:
        return {"gsc_pages": 0, "reason": "no GSC import yet"}
    domains = own_domains(db)
    agg: dict[str, dict] = {}
    for r in db.scalars(select(GscPageRow).where(GscPageRow.import_id == imp_id)):
        u = canonical(r.page, domains)
        if not u:
            continue
        a = agg.setdefault(u, {"impressions": 0, "clicks": 0, "pos_w": 0.0})
        a["impressions"] += r.impressions
        a["clicks"] += r.clicks
        a["pos_w"] += r.position * r.impressions
    for u, a in agg.items():
        _upsert(db, u, "gsc", now, {"impressions": a["impressions"], "clicks": a["clicks"], "position": (a["pos_w"] / a["impressions"]) if a["impressions"] else None})
    db.flush()
    return {"gsc_pages": len(agg)}


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """-> (child sitemap URLs, page URLs)."""
    locs = LOC_RX.findall(xml)
    if "<sitemapindex" in xml[:2000].lower():
        return locs, []
    return [], locs


def refresh_from_sitemap(db: Session) -> dict:
    now = now_utc()
    domains = own_domains(db)
    if not domains:
        return {"sitemap_pages": 0, "reason": "no own-brand domain"}
    root = f"https://www.{domains[0]}/sitemap.xml"
    queue, seen_maps, pages, errors = [root], set(), set(), []
    while queue and len(seen_maps) < MAX_SITEMAPS and len(pages) < MAX_PAGES:
        u = queue.pop(0)
        if u in seen_maps:
            continue
        seen_maps.add(u)
        r = fetcher.fetch_url(u)
        if not r.ok:
            errors.append({"url": u, "outcome": r.outcome, "error": r.error})
            continue
        children, urls = parse_sitemap(r.body.decode("utf-8", errors="replace"))
        queue.extend(c for c in children if c not in seen_maps)
        for x in urls:
            c = canonical(x, domains)
            if c:
                pages.add(c)
    for u in pages:
        _upsert(db, u, "sitemap", now)
    db.flush()
    return {"sitemap_pages": len(pages), "sitemaps_read": len(seen_maps), "errors": errors[:10]}


# --------------------------------------------------------------------------- matching
def stem(tok: str) -> str:
    tok = tok.lower()
    for suf in ("ies", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


COMPOUNDS = [("knock down", "knockdown"), ("knock-down", "knockdown"), ("floor plan", "floorplan"), ("flat pack", "flatpack"), ("duplex", "duplex"), ("town house", "townhouse"), ("town home", "townhome")]


def tokens(text: str) -> list[str]:
    text = text.lower()
    for a, b in COMPOUNDS:
        text = text.replace(a, b)
    return [stem(t) for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in STOP and not t.isdigit()]


def _path_tokens(row: SitePage) -> set[str]:
    return set(tokens(row.path))


def page_coverage(db: Session, terms: str | list[str], state: str | None = None, limit: int = 6, pages: list[SitePage] | None = None) -> list[dict]:
    """Existing pages whose URL path covers the topic. Every non-generic topic word must be present (place words match their aliases)."""
    topic = tokens(" ".join(terms) if isinstance(terms, list) else terms)
    if state:
        topic += [stem(state.lower())]
    core = [t for t in topic if t not in GENERIC] or topic
    if not core:
        return []
    rows = pages if pages is not None else db.scalars(select(SitePage)).all()
    out = []
    for r in rows:
        pt = _path_tokens(r)
        ok = True
        for t in core:
            alias = PLACE_ALIASES.get(t)
            if t in pt or (alias and (alias & pt or (r.state_hint and r.state_hint.lower() in alias))):
                continue
            ok = False
            break
        if ok:
            out.append({"url": r.url, "path": r.path, "kind": r.page_kind, "impressions": r.impressions, "position": round(r.position, 1) if r.position else None, "in_sitemap": "sitemap" in r.sources})
    out.sort(key=lambda x: -(x["impressions"] or 0))
    return out[:limit]


def inventory_summary(db: Session, max_paths: int = 1500) -> dict:
    """Compact picture for the AI: counts by kind, and the paths themselves (largest-first) so it can see what exists."""
    total = db.scalar(select(func.count()).select_from(SitePage)) or 0
    kinds = dict(db.execute(select(func.coalesce(SitePage.page_kind, "other"), func.count()).group_by(SitePage.page_kind)).all())
    rows = db.scalars(select(SitePage).order_by(func.coalesce(SitePage.impressions, 0).desc(), SitePage.path).limit(max_paths)).all()
    return {"total_pages": total, "by_kind": kinds, "sitemap_pages": db.scalar(select(func.count()).select_from(SitePage).where(SitePage.sources.contains(["sitemap"]))) or 0,
            "paths": [{"path": r.path, "kind": r.page_kind, "state": r.state_hint, "impressions": r.impressions} for r in rows], "truncated": total > max_paths}
