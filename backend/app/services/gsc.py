"""Google Search Console workbook import and theme mapping.

Rules from the brief (s5):
* Both tabs are imported; query rows and page rows stay in separate tables. There is no join key and none
  is invented. Impression totals are never added across the two datasets.
* Original values are preserved. The reporting period is import metadata, not a column.
* Re-importing the same data is a no-op (content hash of canonical rows).
* Mapping uses cheap rules first; Claude only classifies leftovers, in batches (see classify_unmapped).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from ..models import (
    AppSetting,
    GscImport,
    GscPageRow,
    GscQueryRow,
    QueryMappingOverride,
    Theme,
    ThemeKeyword,
    ThemeRule,
)

QUERY_HEADERS = ["Query", "Clicks", "Impressions", "CTR", "Position"]
PAGE_HEADERS = ["Page", "Clicks", "Impressions", "CTR", "Position"]
CHUNK = 5000

STATE_WORDS = {
    "VIC": r"melbourne|victoria|\bvic\b|geelong|ballarat|bendigo|warragul|drouin|pakenham|clyde|berwick|cranbourne|werribee|tarneit|truganina|wollert|epping|craigieburn|sunbury|point cook|officer|mickleham|donnybrook|wyndham|casey|frankston|mornington|shepparton|torquay|melton|cobblebank|armstrong creek|rowville|mulgrave",
    "NSW": r"sydney|\bnsw\b|new south wales|newcastle|hunter|central coast|wollongong|illawarra|western sydney|hills district|penrith|campbelltown|oran park|marsden park|box hill nsw|leppington|schofields|gregory hills|kellyville|riverstone|maitland|cessnock",
    "QLD": r"brisbane|queensland|\bqld\b|gold coast|sunshine coast|ipswich|logan|moreton bay|toowoomba|springfield|redland|caboolture|north lakes|townsville|cairns|tweed|yarrabilba|ripley|palmview|coomera|burpengary",
    "SA": r"adelaide|south australia|\bsa\b|mount barker|gawler|munno para|angle vale|two wells|andrews farm|seaford|noarlunga|onkaparinga|playford|roseworthy|aldinga|hindmarsh island",
}
OTHER_STATE_WORDS = r"perth|\bwa\b|western australia|tasmania|\btas\b|hobart|launceston|darwin|\bnt\b|northern territory|canberra|\bact\b"
_STATE_RES = {k: re.compile(v) for k, v in STATE_WORDS.items()}
_OTHER_RE = re.compile(OTHER_STATE_WORDS)


def normalise_query(q: str) -> str:
    q = re.sub(r"\s+", " ", str(q).strip().lower())
    return q[:400]


def state_hint(text: str) -> str | None:
    hits = [k for k, rx in _STATE_RES.items() if rx.search(text)]
    if len(hits) == 1:
        return hits[0]
    if not hits and _OTHER_RE.search(text):
        return "OTH"
    return None  # ambiguous or none


# --------------------------------------------------------------------------- page classification
_PAGE_STATE_SEGMENTS = {
    "melbourne": "VIC", "vic-regional": "VIC", "victoria": "VIC", "vic": "VIC",
    "sydney": "NSW", "northern-nsw": "NSW", "southern-nsw": "NSW", "nsw": "NSW",
    "queensland": "QLD", "qld": "QLD", "south-east-queensland": "QLD",
    "south-australia": "SA", "adelaide": "SA", "sa": "SA",
}
_PAGE_KINDS = [
    ("latest-offers", "offers", None),
    ("house-and-land/packages", "house_and_land_packages", "house-and-land"),
    ("house-and-land/estates", "house_and_land_estates", "house-and-land"),
    ("house-and-land/suburbs", "house_and_land_suburbs", "house-and-land"),
    ("house-and-land", "house_and_land", "house-and-land"),
    ("display-homes-for-sale", "display_homes_for_sale", "display-homes"),
    ("display-homes", "display_homes", "display-homes"),
    ("knockdown-rebuild", "knockdown_rebuild", "knockdown-rebuild"),
    ("townhomes", "townhouses", "townhouses"),
    ("townhouse", "townhouses", "townhouses"),
    ("duplex", "dual_occupancy", "dual-occupancy-duplex"),
    ("dual-occupancy", "dual_occupancy", "dual-occupancy-duplex"),
    ("acreage", "acreage", "acreage"),
    ("first-home-buyer", "first_home_buyer", "first-home-buyers-investors"),
    ("costs-and-finance", "costs_finance", "costs-affordability"),
    ("house-designs", "house_designs", "designs-floorplans"),
    ("home-designs", "house_designs", "designs-floorplans"),
    ("the-metricon-range", "design_ranges", "designs-floorplans"),
    ("bedroom-house-designs", "house_designs_by_bedrooms", "layouts-blocks"),
    ("storey-house-designs", "house_designs_by_storeys", "layouts-blocks"),
    ("narrow", "house_designs_by_block", "layouts-blocks"),
    ("small-house", "house_designs_by_block", "layouts-blocks"),
    ("hampton", "styles", "styles-features"),
    ("lookbook", "styles", "styles-features"),
    ("farmhouse", "styles", "styles-features"),
    ("home-truths", "articles", None),
    ("resource-hub", "articles", None),
    ("documents", "documents", None),
]


def classify_page(url: str) -> dict:
    parts = urlsplit(url)
    path = parts.path.lower().strip("/")
    kind = theme = None
    for needle, k, t in _PAGE_KINDS:
        if needle in path:
            kind, theme = k, t
            break
    if parts.netloc.startswith("files.") or path.endswith(".pdf"):
        kind, theme = "documents", None
    if path == "":
        kind = "home"
    state = None
    for seg in path.split("/"):
        if seg in _PAGE_STATE_SEGMENTS:
            state = _PAGE_STATE_SEGMENTS[seg]
            break
    return {"path": "/" + path if path else "/", "page_kind": kind, "theme_slug": theme, "state_hint": state}


# --------------------------------------------------------------------------- reading the workbook
@dataclass
class ParsedWorkbook:
    queries: list[dict]
    pages: list[dict]
    file_sha256: str
    content_sha256: str


def _num(v: Any, as_int: bool, where: str) -> Any:
    if v is None or v == "":
        raise ValueError(f"missing numeric value ({where})")
    if isinstance(v, bool):
        raise ValueError(f"unexpected boolean ({where})")
    if as_int:
        if float(v) != int(float(v)):
            raise ValueError(f"expected whole number, got {v!r} ({where})")
        return int(v)
    return float(v)


def parse_workbook(path: str | Path) -> ParsedWorkbook:
    import openpyxl

    path = Path(path)
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    def read(ws, headers: list[str], key: str) -> list[dict]:
        it = ws.iter_rows(values_only=True)
        head = [str(c).strip() if c is not None else "" for c in next(it)][:5]
        if head != headers:
            raise ValueError(f"Sheet {ws.title!r}: expected columns {headers}, found {head}")
        out = []
        for i, r in enumerate(it, start=2):
            if r is None or all(c is None for c in r[:5]):
                continue
            where = f"{ws.title} row {i}"
            label = r[0]
            if label is None or str(label).strip() == "":
                raise ValueError(f"empty {key} ({where})")
            out.append({
                "source_row": i, key: str(label),
                "clicks": _num(r[1], True, where), "impressions": _num(r[2], True, where),
                "ctr": _num(r[3], False, where), "position": _num(r[4], False, where),
            })
        return out

    names = wb.sheetnames
    if len(names) < 2:
        raise ValueError("Workbook must contain two tabs (queries, pages)")
    queries = read(wb[names[0]], QUERY_HEADERS, "query")
    pages = read(wb[names[1]], PAGE_HEADERS, "page")
    h = hashlib.sha256()
    for tag, rows, key in (("q", queries, "query"), ("p", pages, "page")):
        for r in sorted(rows, key=lambda x: x[key]):
            h.update(f"{tag}|{r[key]}|{r['clicks']}|{r['impressions']}|{r['ctr']!r}|{r['position']!r}\n".encode())
    return ParsedWorkbook(queries, pages, file_sha, h.hexdigest())


# --------------------------------------------------------------------------- mapping engine
class Mapper:
    """Applies exclusion patterns, admin overrides, then theme rules (lowest priority number first)."""

    def __init__(self, db: Session, design_names: set[str] | None = None):
        self.design_names = design_names or set()
        self.excl = [re.compile(p, re.I) for p in (db.get(AppSetting, "gsc_exclude_patterns") or AppSetting(key="", value=[])).value or []]
        rules = list(db.execute(select(ThemeRule, Theme).join(Theme, Theme.id == ThemeRule.theme_id).where(Theme.active.is_(True))))
        self.rules = sorted(((r.priority, t.sort_order, re.compile(r.pattern, re.I), t.id) for r, t in rules), key=lambda x: (x[0], x[1]))
        self.overrides = {o.query_norm: o for o in db.scalars(select(QueryMappingOverride))}
        self.brand_rx = re.compile(r"\bmetricon\b", re.I)
        self._designs_id = db.scalar(select(Theme.id).where(Theme.slug == "designs-floorplans"))

    def map(self, q_norm: str) -> tuple[int | None, str, bool]:
        """-> (theme_id, method, excluded)"""
        ov = self.overrides.get(q_norm)
        if ov:
            return (None, "admin", True) if ov.excluded else (ov.theme_id, "admin", False)
        if any(rx.search(q_norm) for rx in self.excl):
            return None, "rule", True
        for _p, _s, rx, tid in self.rules:
            if rx.search(q_norm):
                return tid, "rule", False
        # Metricon's own named designs/ranges (taken from the page tab) are design-intent queries
        stem = re.sub(r"\s+(design|designs|home|homes|house|floor ?plan|floorplan|facade|display|by metricon)$", "", q_norm)
        if stem in self.design_names or q_norm in self.design_names:
            return self._designs_id, "rule", False
        return None, "none", False


_DESIGN_PATH = re.compile(r"/(?:house-designs|home-designs|the-metricon-range)/[a-z0-9-]+/([a-z0-9-]+)/?$")


def design_names_from_pages(urls: Iterable[str]) -> set[str]:
    """Named designs/ranges taken from Metricon page paths, e.g. /house-designs/melbourne/lumiere -> 'lumiere'."""
    names = set()
    for u in urls:
        m = _DESIGN_PATH.search(urlsplit(u).path.lower())
        if m:
            n = m.group(1).replace("-", " ")
            n = re.sub(r"\s+\d+$", "", n)  # variants like 'lumiere 30'
            if len(n) >= 4 and n not in {"default", "melbourne", "sydney", "queensland"}:
                names.add(n)
    return names


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def import_workbook(db: Session, path: str | Path, *, file_name: str | None = None, user_id: int | None = None,
                    period_start: date | None = None, period_end: date | None = None, period_note: str | None = None) -> dict:
    """Idempotent: same data (regardless of file bytes/name) returns the existing import."""
    parsed = parse_workbook(path)
    existing = db.scalar(select(GscImport).where(GscImport.content_sha256 == parsed.content_sha256))
    if existing:
        return {"import_id": existing.id, "created": False, "query_rows": existing.query_row_count,
                "page_rows": existing.page_row_count, "message": "Identical data already imported; nothing changed."}
    imp = GscImport(file_name=file_name or Path(path).name, file_sha256=parsed.file_sha256,
                    content_sha256=parsed.content_sha256, reported_period_start=period_start,
                    reported_period_end=period_end, period_note=period_note, imported_by=user_id,
                    query_row_count=len(parsed.queries), page_row_count=len(parsed.pages))
    db.add(imp)
    db.flush()
    mapper = Mapper(db, design_names_from_pages(r["page"] for r in parsed.pages))
    qrows = []
    for r in parsed.queries:
        qn = normalise_query(r["query"])
        theme_id, method, excluded = mapper.map(qn)
        qrows.append({
            "import_id": imp.id, "source_row": r["source_row"], "query": r["query"], "clicks": r["clicks"],
            "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"], "query_norm": qn,
            "theme_id": theme_id, "mapping_method": method, "state_hint": state_hint(qn), "excluded": excluded,
            "has_brand_term": bool(mapper.brand_rx.search(qn)),
        })
    for chunk in _chunks(qrows, CHUNK):
        db.execute(insert(GscQueryRow), chunk)
    prow = []
    for r in parsed.pages:
        c = classify_page(r["page"])
        prow.append({"import_id": imp.id, "source_row": r["source_row"], "page": r["page"], "clicks": r["clicks"],
                     "impressions": r["impressions"], "ctr": r["ctr"], "position": r["position"], **c})
    for chunk in _chunks(prow, CHUNK):
        db.execute(insert(GscPageRow), chunk)
    db.flush()
    return {"import_id": imp.id, "created": True, "query_rows": len(qrows), "page_rows": len(prow),
            "message": "Imported."}


def remap_import(db: Session, import_id: int) -> dict:
    """Re-apply rules and admin overrides (e.g. after editing the taxonomy). Raw values are untouched."""
    pages = db.scalars(select(GscPageRow.page).where(GscPageRow.import_id == import_id))
    mapper = Mapper(db, design_names_from_pages(pages))
    rows = db.execute(select(GscQueryRow.id, GscQueryRow.query_norm, GscQueryRow.mapping_method, GscQueryRow.theme_id)
                      .where(GscQueryRow.import_id == import_id)).all()
    changed = 0
    batch = []
    for rid, qn, method, cur_theme in rows:
        if method == "llm" and qn not in mapper.overrides:
            continue  # keep batch classifications unless an admin overrides
        theme_id, m, excl = mapper.map(qn)
        if m == "none" and method == "llm":
            continue
        batch.append({"id": rid, "theme_id": theme_id, "mapping_method": m, "excluded": excl})
    for chunk in _chunks(batch, CHUNK):
        db.execute(update(GscQueryRow), chunk)  # ORM bulk UPDATE by primary key
    changed = len(batch)
    return {"remapped": changed}


# --------------------------------------------------------------------------- reporting + representative terms
def theme_summary(db: Session, import_id: int) -> list[dict]:
    q = (select(Theme.slug, Theme.name, func.count(GscQueryRow.id), func.coalesce(func.sum(GscQueryRow.impressions), 0),
                func.coalesce(func.sum(GscQueryRow.clicks), 0))
         .join(GscQueryRow, GscQueryRow.theme_id == Theme.id)
         .where(GscQueryRow.import_id == import_id, GscQueryRow.excluded.is_(False))
         .group_by(Theme.id).order_by(func.sum(GscQueryRow.impressions).desc()))
    return [{"theme": s, "name": n, "queries": c, "impressions": int(i), "clicks": int(k)} for s, n, c, i, k in db.execute(q)]


_PLACE_RX = re.compile("|".join(list(STATE_WORDS.values()) + [OTHER_STATE_WORDS, r"near me|australia|\bau\b"]))


def is_generic_term(q_norm: str) -> bool:
    """A head term usable for a state/national Trends request: no place names, short, no digits-only noise."""
    return (not _PLACE_RX.search(q_norm)) and 1 <= len(q_norm.split()) <= 4 and len(q_norm) <= 40


def propose_representative_terms(db: Session, import_id: int, per_theme: int = 10, auto_track: int = 4) -> dict:
    """Pick a small editable set per theme from GSC head terms. Admin-edited rows are never altered."""
    themes = {t.id: t for t in db.scalars(select(Theme))}
    added = 0
    for tid, theme in themes.items():
        rows = db.execute(
            select(GscQueryRow.query_norm, GscQueryRow.impressions)
            .where(GscQueryRow.import_id == import_id, GscQueryRow.theme_id == tid, GscQueryRow.excluded.is_(False))
            .order_by(GscQueryRow.impressions.desc()).limit(400)).all()
        picked, seen_tokens = [], set()
        for qn, imp in rows:
            if not is_generic_term(qn):
                continue
            toks = frozenset(qn.split())
            if toks in seen_tokens:
                continue
            # skip near-duplicates (singular/plural variants) of something already picked
            stem = re.sub(r"s\b", "", qn)
            if any(re.sub(r"s\b", "", p[0]) == stem for p in picked):
                continue
            seen_tokens.add(toks)
            picked.append((qn, imp))
            if len(picked) >= per_theme:
                break
        existing = {k.keyword: k for k in theme.keywords}
        for i, (qn, imp) in enumerate(picked):
            if qn in existing:
                existing[qn].gsc_impressions = imp
                continue
            db.add(ThemeKeyword(theme_id=tid, keyword=qn, source="gsc", tracked=i < auto_track, gsc_impressions=imp))
            added += 1
        # seed keywords get their GSC weight when the same phrase exists
        for kw in theme.keywords:
            if kw.gsc_impressions is None:
                v = db.scalar(select(GscQueryRow.impressions).where(GscQueryRow.import_id == import_id, GscQueryRow.query_norm == kw.keyword))
                if v:
                    kw.gsc_impressions = v
    db.flush()
    return {"keywords_added": added}


def metricon_page_targets(db: Session, import_id: int) -> dict:
    """What the page tab says about Metricon's services, designs and target regions (page tab only)."""
    kinds = db.execute(select(GscPageRow.page_kind, func.count(), func.sum(GscPageRow.impressions), func.sum(GscPageRow.clicks))
                       .where(GscPageRow.import_id == import_id).group_by(GscPageRow.page_kind)
                       .order_by(func.sum(GscPageRow.impressions).desc())).all()
    states = db.execute(select(GscPageRow.state_hint, func.count(), func.sum(GscPageRow.impressions))
                        .where(GscPageRow.import_id == import_id).group_by(GscPageRow.state_hint)).all()
    return {"by_kind": [{"kind": k, "pages": n, "impressions": int(i or 0), "clicks": int(c or 0)} for k, n, i, c in kinds],
            "by_state_hint": [{"state": s, "pages": n, "impressions": int(i or 0)} for s, n, i in states],
            "note": "Page-level GSC figures are a separate dataset from the query tab; they are not additive across tabs."}


def link_theme_pages(db: Session, import_id: int, theme_slug: str, limit: int = 5) -> list[dict]:
    """Metricon pages relevant to a theme, from the page tab (does not rely on any query->page join)."""
    rows = db.scalars(select(GscPageRow).where(GscPageRow.import_id == import_id, GscPageRow.theme_slug == theme_slug,
                                               GscPageRow.page.not_like("%?%")).order_by(GscPageRow.impressions.desc()).limit(limit))
    return [{"page": r.page, "impressions": r.impressions, "clicks": r.clicks, "state": r.state_hint} for r in rows]


# --------------------------------------------------------------------------- Claude-assisted clustering (batched, ambiguous queries only)
def classify_unmapped(db: Session, llm, import_id: int, limit: int = 300, batch_size: int = 100, min_impressions: int = 200) -> dict:
    """Send only the highest-impression unmapped queries, 100 at a time, never one call per query."""
    from pydantic import BaseModel, Field

    themes = {t.slug: t for t in db.scalars(select(Theme).where(Theme.active.is_(True)))}

    class Item(BaseModel):
        i: int
        theme: str = Field(description="a theme slug from the list, or 'none' if it is generic/unclear, or 'off_topic' if it is not about buying/building a home")

    class Batch(BaseModel):
        items: list[Item]

    rows = db.execute(select(GscQueryRow.id, GscQueryRow.query_norm, GscQueryRow.impressions).where(
        GscQueryRow.import_id == import_id, GscQueryRow.theme_id.is_(None), GscQueryRow.excluded.is_(False), GscQueryRow.mapping_method == "none",
        GscQueryRow.impressions >= min_impressions).order_by(GscQueryRow.impressions.desc()).limit(limit)).all()
    stats = {"queries": len(rows), "calls": 0, "mapped": 0, "excluded": 0, "left": 0}
    legend = "\n".join(f"- {slug}: {t.name}" for slug, t in themes.items())
    system = ("You classify Google Search Console queries for an Australian home builder's marketing team. Pick the single best theme slug for each query, 'none' if the "
              "query is too generic or unclear (e.g. just 'home'), or 'off_topic' if it is not about choosing, buying or building a new home (rentals, decorating, "
              "maintenance, jobs, unrelated property news). Named designs/estates/suburbs of new-home buyers belong with the closest theme. Never invent slugs.")
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        user = f"Themes:\n{legend}\n\nQueries (index: text):\n" + "\n".join(f"{n}: {q[1]}" for n, q in enumerate(chunk))
        out = llm.structured(system=system, user=user, schema=Batch, tool_name="submit_classes", max_tokens=4000)
        stats["calls"] += 1
        upd = []
        for it in out.items:
            if not (0 <= it.i < len(chunk)):
                continue
            rid = chunk[it.i][0]
            if it.theme in themes:
                upd.append({"id": rid, "theme_id": themes[it.theme].id, "mapping_method": "llm", "excluded": False})
                stats["mapped"] += 1
            elif it.theme == "off_topic":
                upd.append({"id": rid, "theme_id": None, "mapping_method": "llm", "excluded": True})
                stats["excluded"] += 1
            else:
                stats["left"] += 1
        if upd:
            db.execute(update(GscQueryRow), upd)
        db.commit()
    return stats
