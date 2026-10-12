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
ABS_PAGES = {
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
    url = f"{ABS_BASE}/ABS,{series.dataset},{'1.0.0' if series.dataset == 'BA_GCCSA' else '1.1.3'}/{series.series_key}"
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


def check_all(db: Session) -> dict:
    """Daily release check. Pulls each dataset; a new reference period or changed value creates a release record."""
    ensure_series(db)
    out = {"datasets": {}, "errors": []}
    with _client() as client:
        for dataset in ("BA_GCCSA", "PPI"):
            series_list = list(db.scalars(select(MacroSeries).where(MacroSeries.dataset == dataset, MacroSeries.active.is_(True))))
            try:
                info = abs_release_info(client, dataset)
                probe = series_list[0]
                url = f"{ABS_BASE}/ABS,{dataset},{'1.0.0' if dataset == 'BA_GCCSA' else '1.1.3'}/{probe.series_key}"
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
        return {"pct": None, "reason": "zero baseline"}
    return {"pct": round(float((a.value - b.value) / b.value * 100), 1), "from": float(b.value), "to": float(a.value)}
