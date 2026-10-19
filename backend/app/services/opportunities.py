"""Search opportunities computed from Metricon's GSC export, the page inventory and Google Trends. No AI in here: every figure is arithmetic on stored data.

Two honest limits are carried into every result:
* the GSC query tab and page tab are separate exports - a query cannot be tied to the page that ranked for it, so query-level findings point at
  the *theme's* pages, never at a specific URL claimed to rank;
* positions are the export's averages and the period is whatever the export covers.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import GscImport, GscPageRow, GscQueryRow, RelatedQueryCandidate, SitePage, Theme
from ..timeutil import now_utc
from . import inventory

STATES = ["VIC", "NSW", "QLD", "SA"]
STATE_NAMES = {"VIC": "Victoria", "NSW": "New South Wales", "QLD": "Queensland", "SA": "South Australia"}


def latest_import(db: Session) -> GscImport | None:
    return db.scalar(select(GscImport).order_by(GscImport.id.desc()).limit(1))


def _qrows(db: Session, imp_id: int, branded: bool | None = False) -> list[GscQueryRow]:
    q = select(GscQueryRow).where(GscQueryRow.import_id == imp_id, GscQueryRow.excluded.is_(False))
    if branded is not None:
        q = q.where(GscQueryRow.has_brand_term.is_(branded))
    return list(db.scalars(q))


def ctr_benchmark(rows: list[GscQueryRow], min_impressions: int = 1500) -> dict[int, float]:
    """Impression-weighted CTR by whole position, from Metricon's own unbranded queries. Positions without enough data are omitted."""
    imp, clk = defaultdict(int), defaultdict(int)
    for r in rows:
        b = int(round(r.position))
        if 1 <= b <= 20:
            imp[b] += r.impressions
            clk[b] += r.clicks
    return {b: clk[b] / imp[b] for b in imp if imp[b] >= min_impressions}


def theme_scorecard(db: Session, imp_id: int) -> list[dict]:
    themes = {t.id: t for t in db.scalars(select(Theme))}
    agg: dict[int, dict] = {}
    for r in _qrows(db, imp_id, branded=None):
        if r.theme_id is None:
            continue
        a = agg.setdefault(r.theme_id, {"queries": 0, "impressions": 0, "clicks": 0, "pos_w": 0.0, "top3": 0, "top10": 0, "page2": 0, "branded": 0})
        a["queries"] += 1
        a["impressions"] += r.impressions
        a["clicks"] += r.clicks
        a["pos_w"] += r.position * r.impressions
        if r.has_brand_term:
            a["branded"] += r.impressions
        if r.position <= 3:
            a["top3"] += r.impressions
        if r.position <= 10:
            a["top10"] += r.impressions
        elif r.position <= 20:
            a["page2"] += r.impressions
    pages = defaultdict(lambda: {"n": 0, "imp": 0})
    for p in db.scalars(select(SitePage).where(SitePage.theme_slug.is_not(None))):
        pages[p.theme_slug]["n"] += 1
        pages[p.theme_slug]["imp"] += p.impressions or 0
    out = []
    for tid, a in agg.items():
        t = themes[tid]
        imp = a["impressions"] or 1
        out.append({"id": f"gsc:theme:{t.slug}", "kind": "gsc_theme", "theme": t.name, "theme_slug": t.slug, "queries": a["queries"], "impressions": a["impressions"], "clicks": a["clicks"],
                    "ctr_pct": round(100 * a["clicks"] / imp, 2), "avg_position_weighted": round(a["pos_w"] / imp, 1), "share_impressions_top3_pct": round(100 * a["top3"] / imp, 1),
                    "share_impressions_top10_pct": round(100 * a["top10"] / imp, 1), "share_impressions_page2_pct": round(100 * a["page2"] / imp, 1),
                    "branded_share_pct": round(100 * a["branded"] / imp, 1), "site_pages_in_theme": pages[t.slug]["n"], "site_pages_in_theme_with_impressions": pages[t.slug]["imp"]})
    out.sort(key=lambda x: -x["impressions"])
    return out


def quick_wins(db: Session, imp_id: int, limit: int = 20) -> list[dict]:
    """Unbranded queries sitting just off the first page or low on it, ranked by impressions."""
    rows = _qrows(db, imp_id)
    bench = ctr_benchmark(rows)
    target = bench.get(3)
    pool = [r for r in rows if 4 <= r.position < 20 and r.impressions >= 100]
    pool.sort(key=lambda r: -r.impressions)
    themes = {t.id: t.slug for t in db.scalars(select(Theme))}
    out = []
    for n, r in enumerate(pool[:limit], 1):
        ctr = r.clicks / r.impressions if r.impressions else 0
        extra = round(r.impressions * max(0.0, target - ctr)) if target is not None else None
        out.append({"id": f"gsc:quickwin:{n}", "kind": "gsc_quick_win", "query": r.query, "theme_slug": themes.get(r.theme_id), "state_hint": r.state_hint, "impressions": r.impressions,
                    "clicks": r.clicks, "position": round(r.position, 1), "ctr_pct": round(100 * ctr, 2),
                    "indicative_extra_clicks_if_position_3": extra,
                    "basis": "Metricon's own unbranded CTR at position 3 in this export, applied to this query's impressions. Indicative only." if extra is not None else "Not enough data for a position-3 benchmark."})
    return out


def ctr_underperformers(db: Session, imp_id: int, limit: int = 10) -> list[dict]:
    rows = _qrows(db, imp_id)
    bench = ctr_benchmark(rows)
    out = []
    for r in rows:
        b = bench.get(int(round(r.position)))
        if b is None or r.position > 6 or r.impressions < 300:
            continue
        ctr = r.clicks / r.impressions
        if ctr < 0.6 * b:
            out.append((r.impressions * (b - ctr), r, ctr, b))
    out.sort(key=lambda x: -x[0])
    return [{"id": f"gsc:ctr:{n}", "kind": "gsc_low_ctr", "query": r.query, "impressions": r.impressions, "clicks": r.clicks, "position": round(r.position, 1), "ctr_pct": round(100 * c, 2),
             "benchmark_ctr_pct": round(100 * b, 2), "basis": "Benchmark = Metricon's own unbranded CTR at the same whole position in this export."} for n, (_, r, c, b) in enumerate(out[:limit], 1)]


def content_gaps(db: Session, imp_id: int) -> list[dict]:
    """Theme x state combinations people search for where the inventory has no page for that theme in that state."""
    themes = {t.id: t for t in db.scalars(select(Theme))}
    agg: dict[tuple, dict] = {}
    for r in _qrows(db, imp_id):
        if r.theme_id is None or r.state_hint not in STATES:
            continue
        a = agg.setdefault((r.theme_id, r.state_hint), {"imp": 0, "q": 0, "top": []})
        a["imp"] += r.impressions
        a["q"] += 1
        a["top"].append((r.impressions, r.query, round(r.position, 1)))
    pages = list(db.scalars(select(SitePage)))
    out = []
    for (tid, st), a in agg.items():
        t = themes[tid]
        have = [p for p in pages if p.theme_slug == t.slug and p.state_hint == st]
        a["top"].sort(reverse=True)
        out.append({"id": f"gap:{t.slug}:{st}", "kind": "content_gap_candidate", "theme": t.name, "theme_slug": t.slug, "state": st, "impressions": a["imp"], "queries": a["q"],
                    "top_queries": [{"query": q, "impressions": i, "position": p} for i, q, p in a["top"][:4]],
                    "existing_pages_for_theme_and_state": [{"url": p.url, "impressions": p.impressions} for p in sorted(have, key=lambda p: -(p.impressions or 0))[:5]],
                    "page_count": len(have)})
    out.sort(key=lambda x: (x["page_count"] > 0, -x["impressions"]))
    return out[:30]


def topic_visibility(db: Session, imp_id: int, limit: int = 25) -> list[dict]:
    """Highest-impression unbranded queries where Metricon ranks beyond page one, and what the inventory already has for the topic."""
    rows = [r for r in _qrows(db, imp_id) if r.position > 10 and r.impressions >= 150]
    rows.sort(key=lambda r: -r.impressions)
    pages = list(db.scalars(select(SitePage)))
    out = []
    for n, r in enumerate(rows[:limit], 1):
        cov = inventory.page_coverage(db, r.query, pages=pages)
        out.append({"id": f"gsc:topic:{n}", "kind": "gsc_topic_visibility", "query": r.query, "impressions": r.impressions, "position": round(r.position, 1), "state_hint": r.state_hint,
                    "existing_pages_matching_topic": cov[:4], "topic_has_page": bool(cov)})
    return out


def new_searches(db: Session, imp_id: int | None, limit: int = 30) -> list[dict]:
    """Rising / new related searches found through Google Trends, with Metricon's visibility and page coverage for each."""
    since = now_utc() - timedelta(days=21)
    cands = list(db.scalars(select(RelatedQueryCandidate).where(RelatedQueryCandidate.relevant.is_(True), RelatedQueryCandidate.kind == "rising",
                                                                 RelatedQueryCandidate.review_status != "rejected").order_by(RelatedQueryCandidate.value.desc().nullslast()).limit(150)))
    rows = [(r, set(inventory.tokens(r.query))) for r in (_qrows(db, imp_id, branded=None) if imp_id else [])]
    pages = list(db.scalars(select(SitePage)))
    out, seen = [], set()
    for c in cands:
        if c.query in seen:
            continue
        seen.add(c.query)
        toks = set(inventory.tokens(c.query))
        match = [r for r, rt in rows if toks and toks <= rt]
        imp = sum(r.impressions for r in match)
        pos = (sum(r.position * r.impressions for r in match) / imp) if imp else None
        cov = inventory.page_coverage(db, c.query, pages=pages)
        out.append({"id": f"new:{len(out) + 1}", "kind": "new_search", "query": c.query, "seed": c.seed, "geo": c.geo_key, "rise_pct": c.value, "theme_slug": c.suggested_theme_slug,
                    "first_seen": c.first_seen_at.date().isoformat(), "new_this_period": c.first_seen_at >= since,
                    "metricon_visibility": ({"matching_queries": len(match), "impressions": imp, "avg_position": round(pos, 1)} if match else "no matching queries in the GSC export"),
                    "existing_pages_matching_topic": cov[:4], "topic_has_page": bool(cov)})
    out.sort(key=lambda x: (not x["new_this_period"], bool(x["topic_has_page"]), -(x["rise_pct"] or 0)))
    return out[:limit]


def compute(db: Session) -> dict:
    imp = latest_import(db)
    inv = inventory.inventory_summary(db)
    out = {"gsc_available": imp is not None, "inventory": inv, "gaps": []}
    if imp is None:
        out["gaps"].append("No Search Console export has been imported, so Metricon's own search visibility is unknown.")
        out.update(theme_scorecard=[], quick_wins=[], low_ctr=[], content_gaps=[], topic_visibility=[], new_searches=new_searches(db, None))
        return out
    out.update(import_id=imp.id, period=imp.period_note or f"{imp.reported_period_start} to {imp.reported_period_end}",
               theme_scorecard=theme_scorecard(db, imp.id), quick_wins=quick_wins(db, imp.id), low_ctr=ctr_underperformers(db, imp.id), content_gaps=content_gaps(db, imp.id),
               topic_visibility=topic_visibility(db, imp.id), new_searches=new_searches(db, imp.id))
    if inv["sitemap_pages"] == 0:
        out["gaps"].append("The sitemap has not been read yet, so the page inventory only contains pages that appeared in Search Console. Pages that exist but earn no impressions may be missing.")
    if not out["new_searches"]:
        out["gaps"].append("No rising related searches are stored yet (needs the Google Trends connection and the weekly discovery job).")
    return out
