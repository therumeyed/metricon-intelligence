"""AI search visibility: the segmented Ahrefs Brand Radar workbook (one row per keyword x AI platform answer).

Every figure the dashboard shows is calculated here from the stored answer rows, so it can be traced and re-sliced (by AI platform, by who
was searching). The workbook's own summary sheets are only used to cross-check those calculations at import, and the written parts (insights,
brand narrative, cited-domain types, untracked builders) are kept as supplied. Nothing is estimated: a segment with too few answers is flagged,
never ranked as a winner.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import AiVisImport, AiVisRow

STATES = ["VIC", "NSW", "QLD", "SA"]
OUTSIDE = ["WA", "ACT", "TAS", "NT"]
NO_STATE = "No state named"
REGION_COLUMNS = STATES + [NO_STATE]
CITIES = [("Melbourne", "VIC"), ("Sydney", "NSW"), ("Adelaide", "SA"), ("Gold Coast", "QLD"), ("Brisbane", "QLD"), ("Sunshine Coast", "QLD")]
CITY_RX = {c: re.compile(r"\b" + c.lower().replace(" ", r"\s+") + r"\b") for c, _ in CITIES}
BRANDS = [("Metricon", "metricon_mentioned"), ("G.J. Gardner", "gj_mentioned"), ("Carlisle", "carlisle_mentioned"), ("Simonds", "simonds_mentioned"), ("Porter Davis", "porter_mentioned")]
OUTCOMES = [
    ("won", "Named and linked"), ("named_not_cited", "Named, our site not cited"), ("cited_not_named", "Site cited, brand not named"),
    ("rival_only", "A rival is named, Metricon isn't"), ("nobody", "No builder named (open ground)"), ("other", "Other"),
]
OUTCOME_OF = {
    "Defend – mentioned & cited": "won",
    "Citation gap – named, not cited": "named_not_cited", "Brand – mentioned, not cited": "named_not_cited",
    "Cited, not named": "cited_not_named",
    "Competitor gap – rival named, Metricon absent": "rival_only",
    "Whitespace – no builder named": "nobody",
}
BRAND_TYPES = {"non_branded": ["Non-branded"], "metricon_branded": ["Metricon branded"], "competitor_branded": ["Competitor branded"], "all": None}
REQUIRED = ["Row ID", "Keyword", "AI platform", "Product / topic", "Region", "Brand type", "Excluded", "Metricon mentioned", "Metricon cited", "GJ Gardner mentioned",
            "Carlisle mentioned", "Simonds mentioned", "Opportunity type", "Opportunity score"]


def city_of(keyword: str) -> str | None:
    """The city a search names, read from its wording ('house designs melbourne'). The workbook has no city column. Searches naming no city, or two, return None."""
    k = (keyword or "").lower()
    hits = [c for c, rx in CITY_RX.items() if rx.search(k)]
    return hits[0] if len(hits) == 1 else None


def region_group(region: str) -> str:
    return region if region in STATES else (region if region in OUTSIDE else NO_STATE)


# --------------------------------------------------------------------------- reading the workbook
def _b(v: Any) -> bool:
    return bool(int(v)) if v not in (None, "", "None") else False


def _i(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _d(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _table(ws, header_first: str) -> list[dict]:
    """Rows under the header row whose first cell equals `header_first`, as dicts keyed by header. Stops at the first fully blank row."""
    rows = list(ws.iter_rows(values_only=True))
    start = next((i for i, r in enumerate(rows) if r and r[0] == header_first), None)
    if start is None:
        return []
    hdr = [h for h in rows[start]]
    out = []
    for r in rows[start + 1:]:
        if not any(c is not None for c in r):
            break
        out.append({str(h): c for h, c in zip(hdr, r) if h is not None})
    return out


def _kv(ws, until: int = 14) -> dict:
    out = {}
    for r in ws.iter_rows(min_row=1, max_row=until, values_only=True):
        cells = [c for c in r if c is not None]
        if len(cells) >= 2 and isinstance(cells[0], str) and isinstance(cells[-1], (int, float)):
            out[cells[0]] = cells[-1]
    return out


def parse_workbook(path: str | Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    if "Data" not in wb.sheetnames:
        raise ValueError("The workbook has no 'Data' sheet - is this the AI visibility segmentation file?")
    raw = list(wb["Data"].iter_rows(values_only=True))
    hdr = [str(h) if h is not None else "" for h in raw[0]]
    missing = [c for c in REQUIRED if c not in hdr]
    if missing:
        raise ValueError(f"The Data sheet is missing columns: {', '.join(missing)}")
    ix = {h: i for i, h in enumerate(hdr)}
    g = lambda r, c, d=None: (r[ix[c]] if c in ix and r[ix[c]] is not None else d)  # noqa: E731
    rows = []
    for r in raw[1:]:
        if not any(c is not None for c in r):
            continue
        rows.append(dict(
            row_id=_i(g(r, "Row ID")), keyword=str(g(r, "Keyword", "")), platform=str(g(r, "AI platform", "")), query_format=g(r, "Query format"), topic=str(g(r, "Product / topic", "")),
            region=str(g(r, "Region", "")), journey=g(r, "Journey stage"), brand_type=str(g(r, "Brand type", "")), intent=g(r, "Primary intent"), volume=_i(g(r, "Volume", 0)),
            volume_capped=_i(g(r, "Volume (capped)", g(r, "Volume", 0))), primary_row=_b(g(r, "Primary keyword row", 1)), excluded=_b(g(r, "Excluded", 0)),
            metricon_mentioned=_b(g(r, "Metricon mentioned")), metricon_cited=_b(g(r, "Metricon cited")), gj_mentioned=_b(g(r, "GJ Gardner mentioned")),
            carlisle_mentioned=_b(g(r, "Carlisle mentioned")), simonds_mentioned=_b(g(r, "Simonds mentioned")), porter_mentioned=_b(g(r, "Porter Davis mentioned")),
            opportunity_type=g(r, "Opportunity type"), opportunity_score=float(g(r, "Opportunity score", 0) or 0), competitors_named=g(r, "Competitors named"),
            metricon_urls=g(r, "Metricon URLs cited"), cited_domains=g(r, "Cited domains"), updated_on=_d(g(r, "Updated"))))
    sheets = {n: wb[n] for n in wb.sheetnames}
    out: dict[str, Any] = {"rows": rows, "source_note": None, "published_kpis": {}, "insights": [], "opportunities": [], "narrative": {}, "domains": {}, "untracked": {}, "relevance": {}, "min_rows": 10,
                           "segment_summary": []}
    ins = sheets.get("Insights_Next_Steps")
    if ins is not None:
        out["source_note"] = ins["A2"].value
        out["published_kpis"] = _kv(ins)
        out["insights"] = _table(ins, "Priority")
    if "Top_Opportunities" in sheets:
        out["opportunities"] = _table(sheets["Top_Opportunities"], "Rank")
    if "Brand_Narrative" in sheets:
        ws = sheets["Brand_Narrative"]
        out["narrative"] = {"caption": ws["A2"].value, "rows": _table(ws, "Keyword")}
    if "Cited_Domains" in sheets:
        ws = sheets["Cited_Domains"]
        out["domains"] = {"caption": ws["A2"].value, "rows": _table(ws, "Domain")}
    if "Untracked_Competitors" in sheets:
        ws = sheets["Untracked_Competitors"]
        out["untracked"] = {"caption": ws["A2"].value, "rows": _table(ws, "Builder")}
    if "Segment_Summary" in sheets:
        out["segment_summary"] = _table(sheets["Segment_Summary"], "Dimension")
    if "Assumptions" in sheets:
        for r in sheets["Assumptions"].iter_rows(values_only=True):
            cells = list(r)
            if len(cells) > 2 and isinstance(cells[1], str) and cells[1].startswith("Minimum rows") and isinstance(cells[2], (int, float)):
                out["min_rows"] = int(cells[2])
            if len(cells) > 8 and isinstance(cells[7], str) and isinstance(cells[8], (int, float)):
                out["relevance"][cells[7]] = float(cells[8])
    return out


# --------------------------------------------------------------------------- aggregation (pure)
@dataclass
class Rw:
    keyword: str
    platform: str
    topic: str
    region: str
    group: str
    city: str | None
    journey: str | None
    brand_type: str
    volume_capped: int
    primary_row: bool
    mentioned: bool
    cited: bool
    named: dict
    outcome: str
    score: float
    competitors: str | None


def _outcome(opp: str | None, mentioned: bool, cited: bool) -> str:
    if opp in OUTCOME_OF:
        return OUTCOME_OF[opp]
    if opp is None:
        return "won" if mentioned and cited else "named_not_cited" if mentioned else "cited_not_named" if cited else "other"
    return "other"


def to_rw(r: AiVisRow) -> Rw:
    return Rw(r.keyword, r.platform, r.topic, r.region, region_group(r.region), city_of(r.keyword), r.journey, r.brand_type, r.volume_capped, r.primary_row, r.metricon_mentioned, r.metricon_cited,
              {"Metricon": r.metricon_mentioned, "G.J. Gardner": r.gj_mentioned, "Carlisle": r.carlisle_mentioned, "Simonds": r.simonds_mentioned, "Porter Davis": r.porter_mentioned},
              _outcome(r.opportunity_type, r.metricon_mentioned, r.metricon_cited), r.opportunity_score, r.competitors_named)


def agg(rows: list[Rw], min_rows: int) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    rates = {b: sum(1 for r in rows if r.named[b]) / n for b, _ in BRANDS}
    ranked = sorted(rates.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    if top[1] == 0:
        winner, lead = "No tracked builder", 0.0
    elif top[1] == second[1]:
        winner, lead = "Tie", 0.0
    else:
        winner, lead = top[0], round((top[1] - second[1]) * 100, 1)
    rival = max((v for b, v in rates.items() if b != "Metricon"), default=0)
    rival_name = max(((b, v) for b, v in rates.items() if b != "Metricon"), key=lambda kv: kv[1])[0] if rival > 0 else None
    oc = Counter(r.outcome for r in rows)
    return {"n": n, "mention": rates["Metricon"], "cited": sum(1 for r in rows if r.cited) / n, "rates": rates, "winner": winner, "lead_pts": lead,
            "top_rival": rival_name, "top_rival_rate": rival if rival_name else 0, "low_sample": n < min_rows, "outcomes": {k: oc.get(k, 0) for k, _ in OUTCOMES},
            "volume": sum(r.volume_capped for r in rows if r.primary_row), "score": round(sum(r.score for r in rows), 1)}


def select_rows(db: Session, imp: AiVisImport, brand_type: str = "non_branded", platform: str | None = None) -> list[Rw]:
    q = select(AiVisRow).where(AiVisRow.import_id == imp.id, AiVisRow.excluded.is_(False))
    types = BRAND_TYPES.get(brand_type, BRAND_TYPES["non_branded"])
    if types is not None:
        q = q.where(AiVisRow.brand_type.in_(types))
    if platform:
        q = q.where(AiVisRow.platform == platform)
    return [to_rw(r) for r in db.scalars(q)]


def tier(imp: AiVisImport, topic: str) -> str:
    """core = what Metricon sells; secondary = decision support; awareness = top-of-funnel topics (weights from the workbook's Assumptions sheet)."""
    w = (imp.relevance or {}).get(topic, 0.4)
    return "core" if w >= 0.9 else "secondary" if w >= 0.6 else "awareness"


def topic_order(imp: AiVisImport, rows: list[Rw]) -> list[str]:
    """Commercially most important topics first (the workbook's own relevance weights), then by size."""
    size = Counter(r.topic for r in rows)
    return sorted(size, key=lambda t: (-(imp.relevance or {}).get(t, 0.4), -size[t]))


LIFT_LABEL = {
    "rival_only": "Rivals are named in answers where Metricon isn't",
    "nobody": "Open ground: no builder owns these answers yet",
    "named_not_cited": "AI names Metricon but doesn't cite the website",
    "cited_not_named": "AI cites the website but doesn't name Metricon",
    "other": "Mixed",
}


def overview(db: Session, imp: AiVisImport, brand_type: str = "non_branded", platform: str | None = None) -> dict:
    rows = select_rows(db, imp, brand_type, platform)
    mr = imp.min_rows or 10
    total = agg(rows, mr)
    if not rows:
        return {"empty": True, "filters": {"brand_type": brand_type, "platform": platform}}
    topics = [t for t in topic_order(imp, rows)]
    grid, pockets = [], []
    by_tr = defaultdict(list)
    by_t = defaultdict(list)
    by_tc = defaultdict(list)
    by_tcity = defaultdict(list)
    for r in rows:
        by_tr[(r.topic, r.group)].append(r)
        by_t[r.topic].append(r)
        if r.city:
            by_tc[(r.topic, r.city)].append(r)
            by_tcity[r.topic].append(r)
    city_names = [c for c, _ in CITIES]
    city_grid = []
    for t in topics:
        cells = {c: agg(by_tr.get((t, c), []), mr) for c in REGION_COLUMNS}
        grid.append({"topic": t, "tier": tier(imp, t), "all": agg(by_t[t], mr), "cells": cells,
                     "outside_markets": sum(len(by_tr.get((t, c), [])) for c in OUTSIDE)})
        if by_tcity[t]:
            city_grid.append({"topic": t, "tier": tier(imp, t), "all": agg(by_tcity[t], mr), "cells": {c: agg(by_tc.get((t, c), []), mr) for c in city_names}})
    for (t, g), rs in by_tr.items():
        if g in OUTSIDE or len(rs) < mr:
            continue
        a = agg(rs, mr)
        gaps = {k: a["outcomes"][k] for k in ("rival_only", "nobody", "named_not_cited", "cited_not_named")}
        dom = max(gaps, key=gaps.get)
        kws = sorted(rs, key=lambda r: -r.score)
        seen, ex = set(), []
        for r in kws:
            if r.keyword not in seen:
                seen.add(r.keyword)
                ex.append({"keyword": r.keyword, "volume": r.volume_capped})
            if len(ex) == 3:
                break
        pockets.append({"topic": t, "tier": tier(imp, t), "region": g, "n": a["n"], "score": a["score"], "mention": a["mention"], "cited": a["cited"], "winner": a["winner"], "lead_pts": a["lead_pts"],
                        "top_rival": a["top_rival"], "top_rival_rate": a["top_rival_rate"], "dominant_gap": dom, "dominant_gap_label": LIFT_LABEL[dom], "gap_rows": gaps[dom],
                        "outcomes": a["outcomes"], "examples": ex})
    pockets.sort(key=lambda p: -p["score"])
    awareness = [p for p in pockets if p["tier"] == "awareness"][:5]
    pockets = [p for p in pockets if p["tier"] != "awareness"]
    plat = []
    for p in sorted({r.platform for r in rows}):
        a = agg([r for r in rows if r.platform == p], mr)
        plat.append({"platform": p, "n": a["n"], "mention": a["mention"], "cited": a["cited"], "low_sample": a["low_sample"], "winner": a["winner"]})
    jr = []
    for j in ("Awareness", "Consideration", "Conversion", "Post-purchase", "Brand reputation"):
        rs = [r for r in rows if r.journey == j]
        if rs:
            a = agg(rs, mr)
            jr.append({"stage": j, "n": a["n"], "mention": a["mention"], "cited": a["cited"], "outcomes": a["outcomes"]})
    by_state = []
    for g in STATES + [NO_STATE]:
        rs = [r for r in rows if r.group == g]
        if rs:
            a = agg(rs, mr)
            by_state.append({"region": g, "n": a["n"], "rates": a["rates"], "winner": a["winner"], "lead_pts": a["lead_pts"], "cited": a["cited"], "mention": a["mention"],
                             "outcomes": a["outcomes"], "low_sample": a["low_sample"]})
    by_city = []
    for c, st in CITIES:
        rs = [r for r in rows if r.city == c]
        a = agg(rs, mr)
        by_city.append({"city": c, "state": st, **({"n": 0} if not rs else {"n": a["n"], "rates": a["rates"], "winner": a["winner"], "lead_pts": a["lead_pts"], "cited": a["cited"], "mention": a["mention"],
                                                                         "outcomes": a["outcomes"], "low_sample": a["low_sample"]})})
    kpi = {"answers": total["n"], "mention": total["mention"], "cited": total["cited"], "rates": total["rates"], "outcomes": total["outcomes"]}
    return {"empty": False, "filters": {"brand_type": brand_type, "platform": platform}, "kpi": kpi, "grid": grid, "columns": REGION_COLUMNS, "city_grid": city_grid, "city_columns": [{"city": c, "state": st} for c, st in CITIES], "by_city": by_city,
            "city_note": "Cities are read from the wording of each search (for example 'house designs melbourne'); the workbook has no city column, so searches that name only a suburb or region are not counted here.", "pockets": pockets[:10], "awareness_pockets": awareness,
            "platforms": plat, "journey": jr, "by_state": by_state, "outcomes": [{"key": k, "label": lbl} for k, lbl in OUTCOMES], "min_rows": mr,
            "outside_markets": {g: sum(1 for r in rows if r.region == g) for g in OUTSIDE if any(r.region == g for r in rows)}}


def keywords(db: Session, imp: AiVisImport, topic: str | None, region: str | None, outcome: str | None, brand_type: str = "non_branded", platform: str | None = None, limit: int = 40,
             city: str | None = None) -> list[dict]:
    rows = [r for r in select_rows(db, imp, brand_type, platform) if (not topic or r.topic == topic) and (not region or r.group == region) and (not outcome or r.outcome == outcome)
            and (not city or r.city == city)]
    by = defaultdict(list)
    for r in rows:
        by[r.keyword].append(r)
    out = []
    for k, rs in by.items():
        comps = sorted({c.strip() for r in rs for c in (r.competitors or "").replace(";", ",").split(",") if c.strip() and c.strip() != "None"})
        out.append({"keyword": k, "volume": max(r.volume_capped for r in rs), "platforms": len(rs), "named_on": sum(1 for r in rs if r.mentioned), "cited_on": sum(1 for r in rs if r.cited),
                    "rivals": comps, "score": round(max(r.score for r in rs), 1), "journey": rs[0].journey, "region": rs[0].region, "topic": rs[0].topic})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# --------------------------------------------------------------------------- import
def _checks(parsed: dict, rows: list[dict]) -> list[dict]:
    out = []
    on = [r for r in rows if not r["excluded"]]
    pk = parsed["published_kpis"]
    out.append({"check": "Answers read", "ok": True, "detail": f"{len(rows):,} rows, {len(on):,} on-topic ({len(rows) - len(on)} off-topic rows excluded)."})
    nb = [r for r in on if r["brand_type"] == "Non-branded"]
    if nb:
        mine = {"Metricon named – non-branded queries": sum(r["metricon_mentioned"] for r in nb) / len(nb), "metricon.com.au cited – non-branded queries": sum(r["metricon_cited"] for r in nb) / len(nb),
                "GJ Gardner named – non-branded queries": sum(r["gj_mentioned"] for r in nb) / len(nb)}
        for label, v in mine.items():
            pub = pk.get(label)
            if pub is not None:
                out.append({"check": label, "ok": abs(pub - v) < 0.0005, "detail": f"workbook {pub:.1%} vs recalculated {v:.1%}"})
    pub_n = pk.get("On-topic AI answers analysed")
    if pub_n is not None:
        out.append({"check": "On-topic answers", "ok": int(pub_n) == len(on), "detail": f"workbook {int(pub_n):,} vs rows {len(on):,}"})
    seg = [s for s in parsed["segment_summary"] if s.get("Dimension") == "Product / topic"]
    bad = []
    for s in seg:
        rs = [r for r in on if r["topic"] == s["Segment"]]
        if rs and abs(sum(r["metricon_mentioned"] for r in rs) / len(rs) - float(s["Metricon mention %"] or 0)) > 0.0005:
            bad.append(s["Segment"])
    if seg:
        out.append({"check": "Topic mention rates vs the workbook's Segment_Summary", "ok": not bad, "detail": "all agree" if not bad else f"differ for: {', '.join(bad)}"})
    return out


def import_workbook(db: Session, path: str | Path, file_name: str | None = None) -> dict:
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(AiVisImport).where(AiVisImport.file_sha256 == sha))
    if existing is not None:
        return {"created": False, "import_id": existing.id, "rows": existing.row_count}
    parsed = parse_workbook(path)
    rows = parsed["rows"]
    dates = [r["updated_on"] for r in rows if r["updated_on"]]
    imp = AiVisImport(file_name=file_name or Path(path).name, file_sha256=sha, source_note=parsed["source_note"], period_start=min(dates) if dates else None, period_end=max(dates) if dates else None,
                      row_count=len(rows), min_rows=parsed["min_rows"], relevance=parsed["relevance"], insights=_jsonable(parsed["insights"]), published_kpis=parsed["published_kpis"],
                      opportunities=_jsonable(parsed["opportunities"]), narrative=_jsonable(parsed["narrative"]), domains=_jsonable(parsed["domains"]), untracked=_jsonable(parsed["untracked"]),
                      checks=_checks(parsed, rows))
    db.add(imp)
    db.flush()
    for i in range(0, len(rows), 1000):
        db.bulk_insert_mappings(AiVisRow, [{**r, "import_id": imp.id} for r in rows[i:i + 1000]])
    db.execute(update(AiVisImport).where(AiVisImport.id != imp.id).values(is_current=False))
    imp.is_current = True
    db.flush()
    return {"created": True, "import_id": imp.id, "rows": len(rows), "checks": imp.checks}


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def current(db: Session) -> AiVisImport | None:
    return db.scalar(select(AiVisImport).where(AiVisImport.is_current.is_(True)).order_by(AiVisImport.id.desc()).limit(1))
