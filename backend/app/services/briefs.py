"""Weekly national and state briefs (brief s11).

The model receives *computed facts with IDs* and writes plain-language highlights that cite those IDs. Everything numeric is
checked afterwards against the cited facts; highlights that cite nothing real, or quote numbers that are not in their
evidence, are dropped. If the model is unavailable or fails, no prose is invented: the brief is recorded as unavailable and
the previous brief (with its original date) stays visible.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Brief, BriefHighlight
from ..timeutil import now_utc
from . import llm as llm_mod
from . import queries as Q
from .llm import LLM, LLMError, LLMUnavailable, wrap_document
from .macro import series_view
from .spend import BudgetExceeded

PROMPT_VERSION = "brief-2026-10-a"
SCOPES = ["AU", "VIC", "NSW", "QLD", "SA"]
SCOPE_NAMES = {"AU": "Australia", "VIC": "Victoria", "NSW": "New South Wales", "QLD": "Queensland", "SA": "South Australia"}


class HighlightOut(BaseModel):
    headline: str = Field(description="Plain one-line summary of a supported development")
    geography: str
    brands: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    observation: str = Field(description="What changed, with the comparison period. Use only numbers present in the cited facts.")
    evidence_ids: list[str] = Field(description="IDs of the facts that support this highlight (copy exactly)")
    interpretation: str = Field(description="Why it may matter. Must read as interpretation, not fact.")
    marketing_implication: str = Field(description="A response or investigation for marketing. Do not simply recommend matching a competitor discount.")
    sales_implication: str = Field(description="A comparison, buyer expectation or condition sales should know")
    evidence_limitation: Optional[str] = Field(None, description="Material gaps or uncertainty, if any")


class BriefOut(BaseModel):
    highlights: list[HighlightOut] = Field(default_factory=list, description="Up to five. Fewer is fine when evidence is thin. Empty is acceptable.")
    overall_note: Optional[str] = Field(None, description="One sentence on how complete the evidence is this week")


SYSTEM = """You write the weekly market brief for Metricon's marketing and sales teams, in concise Australian English.

Hard rules:
- Use ONLY the facts supplied. Never invent data, competitor sales, volumes or causes. Do not claim causation from coincident movements.
- Every highlight must cite fact IDs in evidence_ids, copied exactly. Quote only numbers that appear in the cited facts.
- Keep observation (what the data shows), interpretation (why it might matter) and implications separate. Suggested actions can be to investigate or review positioning; never default to 'match the discount'.
- Google Trends values are relative search-interest indices, not volumes or sales. A brand's search spike alone does not show stronger purchase demand.
- Search interest for a state is statewide; do not describe it as a city. Australia-wide items are national data, not an average of states.
- An offer that applies in one region must not be described as a national pattern. Do not claim a pattern from one local offer. Offers are advertised claims by builders, not confirmed sales.
- A claimed value of inclusions is not a cash saving; a deposit arrangement is not a discount; a government grant is not builder funded.
- Mention uncertainty in evidence_limitation when facts are 'rules-extracted', stale, headline-only or partial.
- Up to five highlights. Fewer when evidence is thin. Plain language, no hype, no filler, no urgency that the facts do not support."""


# --------------------------------------------------------------------------- facts
def week_window(week_start: date) -> tuple[datetime, datetime]:

    from ..timeutil import app_tz

    start = datetime.combine(week_start - timedelta(days=7), datetime.min.time(), tzinfo=app_tz())
    return start, now_utc()


def gather_facts(db: Session, scope: str, week_start: date) -> dict:
    since, now = week_window(week_start)
    state = None if scope == "AU" else scope
    facts: list[dict] = []
    gaps: list[str] = []

    # offers: new or materially changed in the window, plus the current picture
    offers = Q.list_offers(db, state=state, include_unknown_geo=False)
    for o in offers:
        d = Q.offer_dict(db, o)
        changed = [v for v in o.versions if v.observed_at >= since]
        if changed and not (o.is_baseline and o.versions and o.versions[0].is_baseline and len(o.versions) == len(changed) == 1):
            v = changed[-1]
            facts.append({"id": f"offer:{o.campaign_id}:v{v.version_no}", "kind": "offer_change", "brand": o.brand.name, "title": o.title, "status": o.status,
                          "geography": d["geography_label"], "value": d["value_summary"], "expiry": d["expiry_label"], "conditions": d["key_conditions"],
                          "changes": [{"field": x["field"], "before": x["before"], "after": x["after"]} for x in v.diff][:8], "first_observed_note": "baseline capture" if v.is_baseline else "new version",
                          "extraction": v.extraction_method, "source_url": o.source_url, "freshness": d["freshness"]})
        elif o.status in ("advertised", "needs_review") and len(facts) < 40:
            facts.append({"id": f"offer:{o.campaign_id}:current", "kind": "current_offer", "brand": o.brand.name, "title": o.title, "geography": d["geography_label"],
                          "value": d["value_summary"], "expiry": d["expiry_label"], "conditions": d["key_conditions"], "extraction": o.versions[-1].extraction_method if o.versions else None,
                          "freshness": d["freshness"], "source_url": o.source_url})
    pp = Q.promo_patterns(db, state)
    facts.append({"id": f"promo_patterns:{scope}", "kind": "promo_patterns", **{k: pp[k] for k in ("mechanisms", "roster_size", "roster_with_current_offers", "roster_missing", "note")}})
    if pp["roster_with_current_offers"] < pp["roster_size"]:
        gaps.append(f"Offers captured for {pp['roster_with_current_offers']} of {pp['roster_size']} monitored brands in this scope.")
    if any(f.get("extraction") == "rules" for f in facts):
        gaps.append("Some offers were extracted by rules without an AI model and need human review.")

    for e in Q.events_list(db, state=state, days=14):
        facts.append({"id": f"event:{e['id']}", "kind": "competitor_event", "brand": e["brand"], "type": e["event_type"], "title": e["title"], "observed": e["observed_at"].date().isoformat(),
                      "date_note": e["date_note"], "state": e["state"], "source_url": e["source_url"]})

    geo = scope
    tf = Q.trend_overview_for_brief(db, geo)
    facts.extend(tf)
    if not tf:
        gaps.append("No Google Trends comparison data is stored for this scope (provider not connected, unsupported geography, or insufficient data).")

    from ..models import MacroSeries

    for ser in db.scalars(select(MacroSeries).where(MacroSeries.geography.in_(["AU", scope]), MacroSeries.active.is_(True))):
        v = series_view(db, ser, 14)
        if not v["latest"]:
            continue
        facts.append({"id": f"macro:{ser.code}:{v['latest']['period']}", "kind": "macro_indicator", "name": ser.name, "geography": ser.geography, "unit": ser.unit, "frequency": ser.frequency,
                      "adjustment": ser.adjustment, "latest_period": v["latest"]["period"], "latest_value": v["latest"]["value"], "comparison": v["comparison"],
                      "release": v["release"], "what_it_measures": ser.description})

    from ..models import NewsItem

    cutoff = since - timedelta(days=7)
    nq = select(NewsItem).where(NewsItem.relevance == "kept", NewsItem.published_at >= cutoff).order_by(NewsItem.published_at.desc()).limit(12)
    for n in db.scalars(nq):
        if state and n.state not in (state, None):
            continue
        facts.append({"id": f"news:{n.id}", "kind": "news", "title": n.title, "publisher": n.publisher, "published": n.published_at.date().isoformat() if n.published_at else None,
                      "source_class": n.source_class, "evidence_level": n.evidence_level, "url": n.canonical_url,
                      "limitation": "Headline only - no article text was accessible." if n.evidence_level == "headline_only" else None})
    gaps.append("News items are headlines from a news aggregator unless marked as article-level evidence.")
    return {"scope": scope, "scope_name": SCOPE_NAMES[scope], "week_start": week_start.isoformat(), "generated_for": now.isoformat(), "facts": facts, "data_gaps": gaps}


def fact_index(facts: dict) -> dict[str, dict]:
    return {f["id"]: f for f in facts["facts"]}


_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?[kKmM]?")


def numbers_supported(text: str, cited: list[dict]) -> list[str]:
    """Numbers in the model's observation that do not appear in any cited fact. Years and ordinals are ignored."""
    blob = json.dumps(cited, default=str)
    blob_nums = {n.replace(",", "").lstrip("$").rstrip("%") for n in _NUM.findall(blob)}
    blob_floats = set()
    for n in blob_nums:
        try:
            blob_floats.add(round(float(n.rstrip("kKmM")), 1))
        except ValueError:
            pass
    bad = []
    for n in _NUM.findall(text):
        raw = n.replace(",", "").lstrip("$").rstrip("%")
        mult = 1.0
        if raw[-1:] in "kK":
            raw, mult = raw[:-1], 1000.0
        elif raw[-1:] in "mM":
            raw, mult = raw[:-1], 1_000_000.0
        try:
            v = float(raw)
        except ValueError:
            continue
        if 1990 <= v <= 2100 and "." not in raw and "%" not in n and "$" not in n:
            continue
        if v <= 12 and "%" not in n and "$" not in n:
            continue  # 'four weeks', 'three brands' style counts
        if round(v * mult, 1) in blob_floats or round(v, 1) in blob_floats or any(abs(v * mult - f) < 0.51 for f in blob_floats):
            continue
        bad.append(n)
    return bad


# --------------------------------------------------------------------------- generation
def generate_brief(db: Session, scope: str, week_start: date, llm: LLM | None, job_run_id: int | None = None) -> Brief:
    brief = Brief(scope=scope, week_start=week_start, status="running" if llm is not None else "unavailable", prompt_version=PROMPT_VERSION, generated_at=now_utc())
    db.add(brief)
    db.commit()  # visible straight away: the page can say "being written", and any failure below leaves a readable reason behind
    if llm is None:
        brief.status = "unavailable"
        brief.error = llm_mod.status().get("reason", "Claude is not connected")
        brief.summary_note = "Brief unavailable: the AI model is not connected. Tables and charts are unaffected."
        return brief
    brief.model = llm.model
    try:
        facts = gather_facts(db, scope, week_start)
        brief.input_facts = facts
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        brief.status, brief.error = "failed", f"collecting facts: {type(e).__name__}: {e}"[:500]
        brief.summary_note = "Brief unavailable: the supporting facts could not be assembled."
        return brief
    user = (f"Scope: {facts['scope_name']} ({scope}). Week commencing {week_start.isoformat()} (Australia/Sydney).\n"
            f"Known data gaps: {facts['data_gaps']}\n\n{wrap_document(json.dumps(facts['facts'], default=str, indent=1), 'computed-facts')}\n\n"
            "Write the highlights now.")
    try:
        out = llm.structured(system=SYSTEM, user=user, schema=BriefOut, tool_name="submit_brief", max_tokens=16000, effort="medium")
    except (LLMError, BudgetExceeded, LLMUnavailable, Exception) as e:  # noqa: BLE001 - AI failure must never fabricate a brief
        db.rollback()
        brief.status, brief.error = "failed", llm_mod.friendly_error(e)[:500]
        brief.summary_note = "Brief unavailable: generation failed."
        return brief
    idx = fact_index(facts)
    kept, dropped = [], []
    for h in out.highlights[:5]:
        cited = [idx[i] for i in h.evidence_ids if i in idx]
        if not cited:
            dropped.append({"headline": h.headline, "reason": "no valid evidence IDs"})
            continue
        bad = numbers_supported(h.observation + " " + h.headline, cited)
        if bad:
            dropped.append({"headline": h.headline, "reason": f"numbers not found in cited facts: {bad[:4]}"})
            continue
        kept.append((h, cited))
    for pos, (h, cited) in enumerate(kept, start=1):
        refs = []
        for f in cited:
            refs.append({"type": f["kind"], "id": f["id"], "label": f.get("title") or f.get("name") or f.get("keyword") or f["kind"],
                         "url": f.get("source_url") or f.get("url")})
        brief.highlights.append(BriefHighlight(position=pos, headline=h.headline, scope={"geography": h.geography, "brands": h.brands, "themes": h.themes},
                                               observation=h.observation, evidence_refs=refs, interpretation=h.interpretation,
                                               marketing_implication=h.marketing_implication, sales_implication=h.sales_implication,
                                               evidence_limitation=h.evidence_limitation))
    brief.status = "ok"
    brief.summary_note = out.overall_note
    brief.input_facts = {**facts, "dropped_highlights": dropped}
    db.flush()
    # only a successful brief becomes the current one for its scope+week
    db.execute(update(Brief).where(Brief.scope == scope, Brief.week_start == week_start, Brief.id != brief.id).values(is_current=False))
    brief.is_current = True
    return brief


def current_brief(db: Session, scope: str) -> dict:
    """Latest successful brief with its own date, plus the status of the most recent attempt (never fabricated)."""
    ok = db.scalar(select(Brief).where(Brief.scope == scope, Brief.status == "ok").order_by(Brief.week_start.desc(), Brief.id.desc()).limit(1))
    last = db.scalar(select(Brief).where(Brief.scope == scope).order_by(Brief.id.desc()).limit(1))
    return {"scope": scope, "scope_name": SCOPE_NAMES[scope], "brief": brief_dict(ok) if ok else None,
            "latest_attempt": ({"status": ("failed" if last.status == "running" and (now_utc() - last.generated_at) > timedelta(minutes=20) else last.status), "generated_at": last.generated_at,
                                "error": (last.error or ("The run stopped before finishing (no result after 20 minutes)." if last.status == "running" and (now_utc() - last.generated_at) > timedelta(minutes=20) else None)),
                                "week_start": last.week_start} if last else None),
            "unavailable": ok is None or (last is not None and last.status != "ok" and last.week_start >= (ok.week_start if ok else last.week_start))}


def brief_dict(b: Brief) -> dict:
    return {"id": b.id, "scope": b.scope, "week_start": b.week_start, "generated_at": b.generated_at, "model": b.model, "prompt_version": b.prompt_version, "status": b.status,
            "note": b.summary_note, "data_gaps": (b.input_facts or {}).get("data_gaps", []),
            "highlights": [{"position": h.position, "headline": h.headline, "scope": h.scope, "observation": h.observation, "evidence": h.evidence_refs, "interpretation": h.interpretation,
                            "marketing_implication": h.marketing_implication, "sales_implication": h.sales_implication, "evidence_limitation": h.evidence_limitation} for h in b.highlights]}


def brief_markdown(b: dict) -> str:
    lines = [f"# Weekly market brief - {SCOPE_NAMES.get(b['scope'], b['scope'])}", "",
             f"Week commencing {b['week_start']} | Generated {b['generated_at']:%d %b %Y %H:%M} UTC | Prompt {b['prompt_version']}", ""]
    if b.get("note"):
        lines += [f"_{b['note']}_", ""]
    for h in b["highlights"]:
        lines += [f"## {h['position']}. {h['headline']}", "", f"**Scope:** {h['scope'].get('geography')}"
                  + (f" | Brands: {', '.join(h['scope']['brands'])}" if h['scope'].get('brands') else "")
                  + (f" | Themes: {', '.join(h['scope']['themes'])}" if h['scope'].get('themes') else ""), "",
                  f"**Observation:** {h['observation']}", "", f"**Interpretation:** {h['interpretation']}", "",
                  f"**Marketing implication:** {h['marketing_implication']}", "", f"**Sales implication:** {h['sales_implication']}", ""]
        if h.get("evidence_limitation"):
            lines += [f"**Evidence limitation:** {h['evidence_limitation']}", ""]
        lines += ["**Evidence:**"] + [f"- {e['label']} (`{e['id']}`){' - ' + e['url'] if e.get('url') else ''}" for e in h["evidence"]] + [""]
    if not b["highlights"]:
        lines += ["No substantive, evidence-backed highlights this week.", ""]
    if b.get("data_gaps"):
        lines += ["## Data gaps", ""] + [f"- {g}" for g in b["data_gaps"]] + [""]
    return "\n".join(lines)
