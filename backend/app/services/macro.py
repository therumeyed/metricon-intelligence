"""Official macro indicators: ABS Building Approvals, ABS Producer Price Indexes (house construction OUTPUT), RBA cash rate target.

Live-verified sources (12 Oct 2026):
* ABS Data API  https://data.api.abs.gov.au/rest/data/ABS,BA_GCCSA,1.0.0/<MEASURE.VALUE.SECTOR.WORK_TYPE.BUILDING_TYPE.TSEST.REGION.FREQ>
    1.1.1.1.110.10.<region>.M = number of dwelling units, total value band, private sector, NEW work, Houses, Original, monthly.
    Seasonally adjusted/trend slices returned no records for this slice, so only Original is ingested and labelled as such.
* ABS Data API  .../ABS,PPI,1.1.3/<MEASURE.INDEX.TYPE.FREQ>  with INDEX '3011 House construction <state>' and TYPE=OUTPUT.
* RBA table A2 (changes in monetary policy) CSV.
Input-cost indexes (e.g. 'House Construction Inputs') are never mixed with the output index.
"""
from __future__ import annotations

import csv
import io
import re
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MacroObservation, MacroRelease, MacroRevision, MacroSeries
from ..timeutil import now_utc

ABS_BASE = "https://data.api.abs.gov.au/rest/data"
RBA_A2 = "https://www.rba.gov.au/statistics/tables/csv/a2-data.csv"
RBA_F5 = "https://www.rba.gov.au/statistics/tables/csv/f5-data.csv"
ABS_VERSIONS = {"BA_GCCSA": "1.0.0", "PPI": "1.1.3", "BUILDING_ACTIVITY": "1.0.0"}
ABS_DATASETS = tuple(ABS_VERSIONS)
RBA_RATE_TITLES = {
    "rba_mortgage_rate_variable_discounted": "Lending rates; Housing loans; Banks; Variable; Discounted; Owner-occupier",
    "rba_mortgage_rate_fixed_3yr": "Lending rates; Housing loans; Banks; 3-year fixed; Owner-occupier",
}
ABS_PAGES = {
    "BUILDING_ACTIVITY": "https://www.abs.gov.au/statistics/industry/building-and-construction/building-activity-australia/latest-release",
    "BA_GCCSA": "https://www.abs.gov.au/statistics/industry/building-and-construction/building-approvals-australia/latest-release",
    "PPI": "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/producer-price-indexes-australia/latest-release",
}
BA_REGIONS = {"AU": "AUS", "NSW": "1", "VIC": "2", "QLD": "3", "SA": "4"}
PPI_INDEX = {"AU": "1451370", "NSW": "1450000", "VIC": "1450001", "QLD": "1450002", "SA": "1450003"}
GEO_NAMES = {"AU": "Australia", "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland", "SA": "South Australia"}


def default_series() -> list[dict]:
    out = []
    for geo, region in BA_REGIONS.items():
        out.append(dict(code=f"abs_ba_private_houses_{geo.lower()}", indicator="house_approvals", name=f"Private-sector house approvals - {GEO_NAMES[geo]}",
                        geography=geo, source="ABS", dataset="BA_GCCSA", series_key=f"1.1.1.1.110.10.{region}.M", unit="Number of houses", frequency="monthly",
                        adjustment="Original (not seasonally adjusted)",
                        description="Number of NEW private-sector HOUSES approved in the month (ABS Building Approvals, building type 'Houses', original data). "
                                    "Excludes apartments, townhouses and public-sector approvals; not seasonally adjusted, so compare with the same month last year.",
                        source_url=ABS_PAGES["BA_GCCSA"]))
    for geo, idx in PPI_INDEX.items():
        out.append(dict(code=f"abs_ppi_house_construction_output_{geo.lower()}", indicator="house_output_ppi", name=f"House construction output price index - {GEO_NAMES[geo]}",
                        geography=geo, source="ABS", dataset="PPI", series_key=f"1.{idx}.OUTPUT.Q", unit="Index points", frequency="quarterly",
                        adjustment="Not seasonally adjusted (index number)",
                        description="ABS Producer Price Index, OUTPUT price index for 3011 House construction: what builders charge, not what their inputs cost. "
                                    "An index number against the ABS reference base, not a dollar price.",
                        source_url=ABS_PAGES["PPI"]))
    for geo, region in BA_REGIONS.items():
        out.append(dict(code=f"abs_commenced_private_houses_{geo.lower()}", indicator="house_commencements", name=f"Private-sector houses commenced - {GEO_NAMES[geo]}",
                        geography=geo, source="ABS", dataset="BUILDING_ACTIVITY", series_key=f"M6.{region}.CUR.1.1.110.10.Q", unit="Number of houses", frequency="quarterly",
                        adjustment="Original (not seasonally adjusted)",
                        description="NEW private-sector HOUSES where construction started in the quarter (ABS Building Activity, 'dwelling units commenced', building type 'Houses', original data). "
                                    "Approvals are intentions; commencements are work actually starting, so this is the closest official read on builders' real workload. Compare with the same quarter last year.",
                        source_url=ABS_PAGES["BUILDING_ACTIVITY"]))
    out.append(dict(code="rba_mortgage_rate_variable_discounted", indicator="mortgage_rate", name="Home loan rate - variable, discounted, owner-occupier", geography="AU", source="RBA", dataset="F5",
                    series_key=RBA_RATE_TITLES["rba_mortgage_rate_variable_discounted"], unit="Per cent per annum", frequency="monthly", adjustment="Not applicable",
                    description="RBA indicator lending rate: the typical discounted variable rate banks charge owner-occupiers on new housing loans (CANSTAR data, RBA table F5). National - rates do not differ by state.",
                    source_url="https://www.rba.gov.au/statistics/tables/#interest-rates"))
    out.append(dict(code="rba_mortgage_rate_fixed_3yr", indicator="mortgage_rate", name="Home loan rate - 3-year fixed, owner-occupier", geography="AU", source="RBA", dataset="F5",
                    series_key=RBA_RATE_TITLES["rba_mortgage_rate_fixed_3yr"], unit="Per cent per annum", frequency="monthly", adjustment="Not applicable",
                    description="RBA indicator lending rate: the typical 3-year fixed rate banks charge owner-occupiers on new housing loans (CANSTAR data, RBA table F5). National.",
                    source_url="https://www.rba.gov.au/statistics/tables/#interest-rates"))
    out.append(dict(code="rba_cash_rate_target", indicator="cash_rate", name="Cash rate target", geography="AU", source="RBA", dataset="A2", series_key="ARBAMPNCRT",
                    series_id="ARBAMPNCRT", unit="Per cent", frequency="as announced", adjustment="Not applicable",
                    description="Reserve Bank of Australia cash rate target as announced. A single national rate: it does not differ by state.",
                    source_url="https://www.rba.gov.au/statistics/cash-rate/"))
    return out


def ensure_series(db: Session) -> int:
    n = 0
    have = {s.code for s in db.scalars(select(MacroSeries))}
    for d in default_series():
        if d["code"] not in have:
            db.add(MacroSeries(**d))
            n += 1
    db.flush()
    return n


# --------------------------------------------------------------------------- period helpers
def period_end(label: str) -> date | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})", label)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, monthrange(y, mo)[1])
    m = re.fullmatch(r"(\d{4})-Q([1-4])", label)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        mo = q * 3
        return date(y, mo, monthrange(y, mo)[1])
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", label)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _client() -> httpx.Client:
    from .fetcher import user_agent
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), headers={"User-Agent": user_agent()}, follow_redirects=True)


def parse_sdmx_csv(text: str) -> list[tuple[str, Decimal, str | None]]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        tp, v = r.get("TIME_PERIOD"), r.get("OBS_VALUE")
        if not tp or v in (None, ""):
            continue
        rows.append((tp, Decimal(v), r.get("BASE_PERIOD") or None))
    return rows


def abs_release_info(client: httpx.Client, dataset: str) -> dict:
    """Best-effort read of the release page for reference period, release date and next release (the data API has none of them)."""
    try:
        r = client.get(ABS_PAGES[dataset])
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
        m = re.search(r"Reference period ([A-Z][a-z]+ \d{4}) Released (\d{1,2}/\d{1,2}/\d{4})", text)
        nxt = re.search(r"Next Release (\d{1,2}/\d{1,2}/\d{4})", text)
        return {"reference_period_text": m.group(1) if m else None, "release_date": m.group(2) if m else None,
                "next_release": nxt.group(1) if nxt else None, "page": ABS_PAGES[dataset]}
    except httpx.HTTPError as e:
        return {"error": str(e)[:200]}


# --------------------------------------------------------------------------- ingest
def upsert_observations(db: Session, series: MacroSeries, rows: list[tuple[str, Decimal, str | None]], release: MacroRelease | None) -> dict:
    existing = {o.period: o for o in db.scalars(select(MacroObservation).where(MacroObservation.series_id_fk == series.id))}
    new = revised = 0
    now = now_utc()
    for period, value, base in rows:
        o = existing.get(period)
        if o is None:
            db.add(MacroObservation(series_id_fk=series.id, period=period, period_end=period_end(period), value=value, release_id=release.id if release else None,
                                    first_retrieved_at=now, last_retrieved_at=now))
            new += 1
        else:
            o.last_retrieved_at = now
            if o.value != value:
                db.add(MacroRevision(observation_id=o.id, previous_value=o.value, new_value=value, release_id=release.id if release else None))
                o.value, o.revision_count = value, (o.revision_count or 0) + 1
                revised += 1
        if base and not series.coverage_note:
            series.coverage_note = f"Base period (as published): {base}"
    db.flush()
    return {"new": new, "revised": revised}


def ingest_abs_series(db: Session, series: MacroSeries, client: httpx.Client, release: MacroRelease | None, start: str | None = None) -> dict:
    start = start or ("2018-01" if series.frequency == "monthly" else "2018-Q1")
    url = f"{ABS_BASE}/ABS,{series.dataset},{ABS_VERSIONS[series.dataset]}/{series.series_key}"
    r = client.get(url, params={"startPeriod": start}, headers={"Accept": "application/vnd.sdmx.data+csv"})
    if r.status_code == 404 or r.text.strip().startswith("NoRecordsFound"):
        return {"new": 0, "revised": 0, "note": "no records returned by ABS for this series key"}
    r.raise_for_status()
    rows = parse_sdmx_csv(r.text)
    return upsert_observations(db, series, rows, release)


def ingest_rba(db: Session, series: MacroSeries, client: httpx.Client, release_holder: dict) -> dict:
    r = client.get(RBA_A2)
    r.raise_for_status()
    text = r.text.lstrip("﻿")
    pub = re.search(r"^Publication date,([^,]+)", text, re.M)
    release_holder["publication_date"] = datetime.strptime(pub.group(1), "%d-%b-%Y").date() if pub else None
    rows = []
    for line in csv.reader(io.StringIO(text)):
        if len(line) < 3:
            continue
        try:
            d = datetime.strptime(line[0].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        try:
            v = Decimal(line[2].strip())  # ranges from before 1990 ("17.00 to 17.50") are skipped
        except Exception:  # noqa: BLE001
            continue
        rows.append((d.isoformat(), v, None))
    release_holder["latest"] = rows[-1][0] if rows else None
    return upsert_observations(db, series, rows, release_holder.get("release"))


def parse_rba_table(text: str, title: str) -> tuple[list[tuple[str, Decimal, str | None]], str | None, date | None]:
    """RBA statistical-table CSV (F-series layout): find the column by its exact title, read dd-Mon-yyyy rows. -> (rows by month, series id, publication date)."""
    text = text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    head = next((r for r in rows if r and r[0].strip() == "Title"), None)
    if head is None or title not in head:
        raise ValueError(f"column {title!r} not found in RBA table")
    col = head.index(title)
    sid = next((r[col].strip() for r in rows if r and r[0].strip() == "Series ID" and len(r) > col), None)
    pub = next((r[col].strip() for r in rows if r and r[0].strip() == "Publication date" and len(r) > col), None)
    out = []
    for r in rows:
        if len(r) <= col:
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d-%b-%Y").date()
            v = Decimal(r[col].strip())
        except Exception:  # noqa: BLE001
            continue
        out.append((d.strftime("%Y-%m"), v, None))
    return out[-120:], sid, (datetime.strptime(pub, "%d-%b-%Y").date() if pub else None)


def ingest_rba_f5(db: Session, series: MacroSeries, client: httpx.Client) -> dict:
    r = client.get(RBA_F5)
    r.raise_for_status()
    rows, sid, pub = parse_rba_table(r.text, series.series_key)
    if sid and not series.series_id:
        series.series_id = sid
    latest = rows[-1][0] if rows else None
    rel = None
    if latest:
        rel = db.scalar(select(MacroRelease).where(MacroRelease.source == "RBA", MacroRelease.dataset == "F5", MacroRelease.release_key == latest))
        if rel is None:
            rel = MacroRelease(source="RBA", dataset="F5", release_key=latest, reference_period=latest, publication_date=pub, url=RBA_F5)
            db.add(rel)
            db.flush()
    return {**upsert_observations(db, series, rows, rel), "latest_period": latest}


def check_all(db: Session) -> dict:
    """Daily release check. Pulls each dataset; a new reference period or changed value creates a release record."""
    ensure_series(db)
    out = {"datasets": {}, "errors": []}
    with _client() as client:
        for dataset in ABS_DATASETS:
            series_list = list(db.scalars(select(MacroSeries).where(MacroSeries.dataset == dataset, MacroSeries.active.is_(True))))
            try:
                info = abs_release_info(client, dataset)
                probe = series_list[0]
                url = f"{ABS_BASE}/ABS,{dataset},{ABS_VERSIONS[dataset]}/{probe.series_key}"
                r = client.get(url, params={"lastNObservations": 1}, headers={"Accept": "application/vnd.sdmx.data+csv"})
                r.raise_for_status()
                latest = parse_sdmx_csv(r.text)[0][0]
                rel = db.scalar(select(MacroRelease).where(MacroRelease.source == "ABS", MacroRelease.dataset == dataset, MacroRelease.release_key == latest))
                if rel is None:
                    pd = None
                    if info.get("release_date"):
                        try:
                            pd = datetime.strptime(info["release_date"], "%d/%m/%Y").date()
                        except ValueError:
                            pd = None
                    rel = MacroRelease(source="ABS", dataset=dataset, release_key=latest, reference_period=latest, publication_date=pd, url=ABS_PAGES[dataset], detail=info)
                    db.add(rel)
                    db.flush()
                res = {"latest_period": latest, "new_release": rel.retrieved_at is not None and (now_utc() - rel.retrieved_at).total_seconds() < 60, "series": {}}
                for s in series_list:
                    try:
                        res["series"][s.code] = ingest_abs_series(db, s, client, rel)
                    except Exception as e:  # noqa: BLE001 - one series never blocks another
                        out["errors"].append({"series": s.code, "error": f"{type(e).__name__}: {e}"[:200]})
                out["datasets"][dataset] = res
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                out["errors"].append({"dataset": dataset, "error": f"{type(e).__name__}: {e}"[:200]})
        for s in list(db.scalars(select(MacroSeries).where(MacroSeries.dataset == "F5", MacroSeries.active.is_(True)))):
            try:
                out["datasets"][f"RBA_F5:{s.code}"] = ingest_rba_f5(db, s, client)
                db.commit()
            except Exception as e:  # noqa: BLE001
                db.rollback()
                out["errors"].append({"series": s.code, "error": f"{type(e).__name__}: {e}"[:200]})
        try:
            s = db.scalar(select(MacroSeries).where(MacroSeries.code == "rba_cash_rate_target"))
            holder: dict = {}
            tmp = ingest_rba(db, s, client, holder)
            latest = holder.get("latest")
            rel = db.scalar(select(MacroRelease).where(MacroRelease.source == "RBA", MacroRelease.dataset == "A2", MacroRelease.release_key == (latest or "")))
            if rel is None and latest:
                rel = MacroRelease(source="RBA", dataset="A2", release_key=latest, reference_period=latest, publication_date=holder.get("publication_date"), url=RBA_A2)
                db.add(rel)
            out["datasets"]["RBA_A2"] = {"latest_change": latest, **tmp}
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            out["errors"].append({"dataset": "RBA_A2", "error": f"{type(e).__name__}: {e}"[:200]})
    return out


# --------------------------------------------------------------------------- reading
def series_view(db: Session, series: MacroSeries, months_back: int = 60) -> dict:
    obs = list(db.scalars(select(MacroObservation).where(MacroObservation.series_id_fk == series.id).order_by(MacroObservation.period_end)))
    pts = [{"period": o.period, "period_end": o.period_end.isoformat() if o.period_end else None, "value": float(o.value), "revisions": o.revision_count} for o in obs]
    rel = db.scalar(select(MacroRelease).where(MacroRelease.source == series.source, MacroRelease.dataset == series.dataset).order_by(MacroRelease.id.desc()).limit(1))
    latest = obs[-1] if obs else None
    comp: dict = {}
    if latest and series.frequency == "monthly" and len(obs) > 12:
        prev, yago = obs[-2], obs[-13]
        comp = {"vs_previous_period": _pct(latest, prev), "vs_year_ago": _pct(latest, yago), "previous_label": prev.period, "year_ago_label": yago.period}
    elif latest and series.frequency == "quarterly" and len(obs) > 4:
        prev, yago = obs[-2], obs[-5]
        comp = {"vs_previous_period": _pct(latest, prev), "vs_year_ago": _pct(latest, yago), "previous_label": prev.period, "year_ago_label": yago.period}
    elif latest and series.indicator == "cash_rate" and len(obs) > 1:
        comp = {"previous_value": float(obs[-2].value), "previous_change_date": obs[-2].period}
    lag = None
    if latest and latest.period_end and rel and rel.publication_date and series.source == "ABS":
        lag = (rel.publication_date - latest.period_end).days
    return {"code": series.code, "indicator": series.indicator, "name": series.name, "geography": series.geography, "unit": series.unit,
            "frequency": series.frequency, "adjustment": series.adjustment, "description": series.description, "source": series.source,
            "dataset": series.dataset, "series_key": series.series_key, "series_id": series.series_id, "source_url": series.source_url,
            "coverage_note": series.coverage_note, "points": pts[-months_back:] if series.frequency != "as announced" else pts, "latest": pts[-1] if pts else None,
            "comparison": comp, "release": {"publication_date": rel.publication_date.isoformat() if rel and rel.publication_date else None,
                                            "reference_period": rel.reference_period if rel else None,
                                            "first_retrieved": rel.retrieved_at.isoformat() if rel else None,
                                            "publication_lag_days": lag} if rel else None,
            "next_release": (rel.detail or {}).get("next_release") if rel else None,
            "last_retrieved": obs[-1].last_retrieved_at.isoformat() if obs else None}


def _pct(a: MacroObservation, b: MacroObservation) -> dict:
    if b.value == 0:
        return {"pct": None, "diff": None, "reason": "zero baseline"}
    return {"pct": round(float((a.value - b.value) / b.value * 100), 1), "diff": round(float(a.value - b.value), 2), "from": float(b.value), "to": float(a.value)}


# --------------------------------------------------------------------------- reading the conditions (rules, not forecasts)
GEOS = ["AU", "VIC", "NSW", "QLD", "SA"]
RULES = {
    "house_approvals": "Last 3 months of private house approvals vs the same 3 months a year earlier: +5% or more = supportive, -5% or less = weaker, otherwise flat.",
    "house_commencements": "Latest quarter of private houses commenced vs the same quarter a year earlier: +5% or more = supportive, -5% or less = weaker, otherwise flat.",
    "house_output_ppi": "House construction output price index vs a year earlier: under +2% = steady, +5% or more = prices rising quickly (headwind for affordability), otherwise moderate.",
    "cash_rate": "Cash rate target now vs 12 months ago: lower = easing (supportive), higher = tighter (headwind), same = unchanged.",
    "mortgage_rate": "Discounted variable owner-occupier home loan rate vs 12 months ago: down 0.25 pts or more = supportive, up 0.25 pts or more = headwind, otherwise little changed.",
}
USE = {
    "house_approvals": {"supportive": "More new homes are heading toward construction than a year ago. Check Offers to see which builders are leaning in, and make sure the packages and display homes you promote are the ones you want to sell.",
                        "weaker": "Fewer approvals than a year ago. Expect builders to compete harder for the buyers still active – look at Offers and Competitor activity for new incentives before reacting.",
                        "flat": "Approvals are close to last year's level. No market-wide tailwind or headwind to plan around – position on product and price, not on the market."},
    "house_commencements": {"supportive": "More builds are actually starting than a year ago, which is the clearest official sign of builder workload. Trade capacity and lead times may tighten – a good moment to talk about build-time certainty.",
                            "weaker": "Fewer builds are starting than a year ago. Builders may be carrying spare capacity, which can show up as sharper offers – check Offers.",
                            "flat": "Starts are close to last year's level."},
    "house_output_ppi": {"supportive": "Builders' prices are barely moving. Price-certainty messaging has less to push against.",
                         "headwind": "Builders' prices are rising quickly. Buyers will feel affordability pressure – fixed-price, price-hold and inclusions messaging matters more.",
                         "neutral": "Builders' prices are rising at a moderate pace."},
    "cash_rate": {"supportive": "Rates are lower than a year ago. Buyers' borrowing power is better than it was – finance and 'ready to build' messaging can lean on that.",
                  "headwind": "Rates are higher than a year ago. Expect more buyer hesitation and more questions about repayments – lead with certainty and total cost.",
                  "neutral": "The cash rate is unchanged on a year ago."},
    "mortgage_rate": {"supportive": "Home loan rates are lower than a year ago. Repayment worries should be easing – a good moment to talk to buyers who paused.",
                      "headwind": "Home loan rates are higher than a year ago. Buyers feel it in repayments – make the monthly cost of the home easy to see.",
                      "neutral": "Home loan rates are little changed on a year ago."},
}


def _obs(db: Session, series: MacroSeries) -> list[MacroObservation]:
    return list(db.scalars(select(MacroObservation).where(MacroObservation.series_id_fk == series.id).order_by(MacroObservation.period_end)))


def rolling_yoy(obs: list[MacroObservation], n: int, step: int) -> dict | None:
    """Sum of the latest n periods vs the same n periods one year earlier (step = periods per year). None when history or continuity is missing."""
    if len(obs) < n + step:
        return None
    cur, base = obs[-n:], obs[-n - step:len(obs) - step]
    if len(base) < n or any(o.value is None for o in cur + base):
        return None
    a, b = sum(float(o.value) for o in cur), sum(float(o.value) for o in base)
    if b == 0:
        return None
    return {"pct": round((a - b) / b * 100, 1), "current": a, "base": b, "current_label": f"{cur[0].period} to {cur[-1].period}" if n > 1 else cur[-1].period,
            "base_label": f"{base[0].period} to {base[-1].period}" if n > 1 else base[-1].period}


def _stance_volume(pct: float | None) -> str:
    return "unknown" if pct is None else "supportive" if pct >= 5 else "weaker" if pct <= -5 else "flat"


def _value_a_year_ago(obs: list[MacroObservation], ref: date) -> MacroObservation | None:
    target = ref.replace(year=ref.year - 1)
    prior = [o for o in obs if o.period_end and o.period_end <= target]
    return prior[-1] if prior else None


def read_indicator(db: Session, series: MacroSeries) -> dict:
    obs = _obs(db, series)
    ind = series.indicator
    base = {"indicator": ind, "series": series.name, "geography": series.geography, "rule": RULES.get(ind), "stance": "unknown", "basis": "Not enough history to compare.", "use": None, "period": obs[-1].period if obs else None}
    if not obs:
        return base
    stance, basis = "unknown", base["basis"]
    if ind == "house_approvals":
        r = rolling_yoy(obs, 3, 12)
        if r:
            stance = _stance_volume(r["pct"])
            basis = f"{r['current_label']}: {r['current']:,.0f} house approvals vs {r['base']:,.0f} a year earlier ({r['pct']:+.1f}%)."
    elif ind == "house_commencements":
        r = rolling_yoy(obs, 1, 4)
        if r:
            stance = _stance_volume(r["pct"])
            basis = f"{r['current_label']}: {r['current']:,.0f} houses commenced vs {r['base']:,.0f} in {r['base_label']} ({r['pct']:+.1f}%)."
    elif ind == "house_output_ppi":
        r = rolling_yoy(obs, 1, 4)
        if r:
            stance = "supportive" if r["pct"] < 2 else "headwind" if r["pct"] >= 5 else "neutral"
            basis = f"{r['current_label']}: index {r['current']:.1f} vs {r['base']:.1f} in {r['base_label']} ({r['pct']:+.1f}%)."
    elif ind in ("cash_rate", "mortgage_rate"):
        latest = obs[-1]
        ya = _value_a_year_ago(obs, latest.period_end or now_utc().date())
        if ya is not None:
            diff = float(latest.value) - float(ya.value)
            tol = 0.25 if ind == "mortgage_rate" else 0.01
            stance = "supportive" if diff <= -tol else "headwind" if diff >= tol else "neutral"
            basis = f"{float(latest.value):.2f}% now vs {float(ya.value):.2f}% a year earlier ({diff:+.2f} pts)."
    base.update(stance=stance, basis=basis, use=USE.get(ind, {}).get(stance if stance in USE.get(ind, {}) else ("flat" if "flat" in USE.get(ind, {}) else "neutral")) if stance != "unknown" else None)
    return base


def conditions(db: Session, geo: str) -> dict:
    """One plain read per indicator for the chosen geography (national series where no state series exists), with the rule used."""
    ensure_series(db)
    by = {}
    for s in db.scalars(select(MacroSeries).where(MacroSeries.active.is_(True), MacroSeries.geography.in_(["AU", geo]))):
        key = s.indicator if s.indicator != "mortgage_rate" else None
        if key is None:
            if s.code != "rba_mortgage_rate_variable_discounted":
                continue
            key = "mortgage_rate"
        if key not in by or s.geography == geo:
            by[key] = s
    order = ["house_approvals", "house_commencements", "house_output_ppi", "cash_rate", "mortgage_rate"]
    items = [read_indicator(db, by[k]) for k in order if k in by]
    for it in items:
        it["scope"] = "national series" if it["geography"] == "AU" and geo != "AU" else it["geography"]
    n = {k: sum(1 for i in items if i["stance"] == k) for k in ("supportive", "headwind", "weaker", "neutral", "flat", "unknown")}
    pos, neg = n["supportive"], n["headwind"] + n["weaker"]
    known = len(items) - n["unknown"]
    if not known:
        summary = "Not enough data stored yet to read the conditions."
    elif pos > neg:
        summary = f"{pos} of {known} indicators are supportive and {neg} point the other way. Conditions lean favourable; the story is mostly about competitors, not the market."
    elif neg > pos:
        summary = f"{neg} of {known} indicators point to a tougher market and {pos} are supportive. Expect buyers to hesitate and competitors to sharpen offers."
    else:
        summary = f"Mixed: {pos} supportive and {neg} unfavourable out of {known}. No clear market-wide tailwind or headwind."
    return {"geo": geo, "summary": summary, "items": items, "note": "Plain rules applied to official statistics – a reading of direction, not a forecast. Rules are shown on each line."}


def compare_states(db: Session) -> dict:
    """The same indicator side by side for every geography we hold, so users can see where conditions are firmer."""
    ensure_series(db)
    rows = []
    for ind, n, step, label in (("house_approvals", 3, 12, "Private house approvals, 3 months vs a year earlier"), ("house_commencements", 1, 4, "Private houses commenced, latest quarter vs a year earlier"),
                                ("house_output_ppi", 1, 4, "House construction output prices vs a year earlier")):
        cells = {}
        for s in db.scalars(select(MacroSeries).where(MacroSeries.indicator == ind, MacroSeries.active.is_(True))):
            r = rolling_yoy(_obs(db, s), n, step)
            cells[s.geography] = {"pct": r["pct"], "period": r["current_label"], "stance": (_stance_volume(r["pct"]) if ind != "house_output_ppi" else ("supportive" if r["pct"] < 2 else "headwind" if r["pct"] >= 5 else "neutral"))} if r else None
        rows.append({"indicator": ind, "label": label, "cells": {g: cells.get(g) for g in GEOS}})
    return {"geos": GEOS, "rows": rows}


def release_calendar(db: Session) -> list[dict]:
    """When each dataset was last published and, where the source says, when the next release is due. Dates the source does not publish are left blank."""
    out = []
    seen = set()
    for s in db.scalars(select(MacroSeries).where(MacroSeries.active.is_(True)).order_by(MacroSeries.id)):
        key = (s.source, s.dataset)
        if key in seen:
            continue
        seen.add(key)
        rel = db.scalar(select(MacroRelease).where(MacroRelease.source == s.source, MacroRelease.dataset == s.dataset).order_by(MacroRelease.id.desc()).limit(1))
        nxt = (rel.detail or {}).get("next_release") if rel else None
        nxt_iso = None
        if nxt:
            try:
                nxt_iso = datetime.strptime(nxt, "%d/%m/%Y").date().isoformat()
            except ValueError:
                nxt_iso = None
        out.append({"source": s.source, "dataset": s.dataset, "indicator": s.indicator, "name": {"house_approvals": "ABS Building Approvals", "house_commencements": "ABS Building Activity",
                                                                                                  "house_output_ppi": "ABS Producer Price Indexes", "cash_rate": "RBA cash rate decisions",
                                                                                                  "mortgage_rate": "RBA indicator lending rates"}.get(s.indicator, s.name),
                    "latest_period": rel.reference_period if rel else None, "published": rel.publication_date.isoformat() if rel and rel.publication_date else None, "next_release": nxt_iso,
                    "url": s.source_url})
    out.sort(key=lambda r: (r["next_release"] is None, r["next_release"] or ""))
    return out
