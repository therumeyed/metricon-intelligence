"""Verification of extracted offers against the fetched source text (brief s11: every money amount, eligibility
rule or date must reference supporting text or remain unknown)."""
from __future__ import annotations

import re
from datetime import date

from .schema import Benefit, Eligibility, ExtractedOffer, GeoScope, OfferDates, PageExtraction

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_RX = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?{_MONTH_RX}\.?,?\s*(\d{{4}}|\d{{2}})?\b", re.I)
_MDY = re.compile(rf"\b{_MONTH_RX}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?\b", re.I)
_NUM = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def norm(text: str) -> str:
    t = text.lower()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-").replace(" ", " ")
    t = re.sub(r"[^a-z0-9$%]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def quote_in_text(quote: str | None, corpus_norm: str) -> bool:
    if not quote:
        return False
    q = norm(quote)
    if len(q) < 4:
        return False
    if q in corpus_norm:
        return True
    # tolerate model trimming/ellipses: every ~6-word window must be present, and most windows must match
    words = q.split()
    if len(words) < 6:
        return False
    windows = [" ".join(words[i:i + 5]) for i in range(0, len(words) - 4, 3)]
    hits = sum(1 for w in windows if w in corpus_norm)
    return hits / len(windows) >= 0.8


def money_values(text: str) -> list[float]:
    """All dollar figures in a text, with K/M suffixes expanded: '$82K' -> 82000."""
    out = []
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kKmM]\b|\s?(?:thousand|million))?", text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        suf = (m.group(2) or "").strip().lower()
        if suf.startswith("k") or suf == "thousand":
            v *= 1_000
        elif suf.startswith("m") or suf == "million":
            v *= 1_000_000
        out.append(v)
    # percentages are not dollars; bare 'XX,XXX' figures next to a dollar word are rare enough to ignore
    return out


def amount_supported(amount: float | None, evidence: str) -> bool:
    if amount is None:
        return True
    return any(abs(v - amount) < 0.5 for v in money_values(evidence))


def find_dates(text: str, default_year: int | None = None) -> list[tuple[date, bool]]:
    """Dates in text -> (date, year_was_stated)."""
    found: list[tuple[date, bool]] = []

    def add(d: int, mo: int, y: int | None):
        stated = y is not None
        if y is None:
            y = default_year
        if y is None:
            return
        if y < 100:
            y += 2000
        try:
            found.append((date(y, mo, d), stated))
        except ValueError:
            pass

    for m in _ISO.finditer(text):
        add(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    for m in _DMY.finditer(text):
        add(int(m.group(1)), _MONTHS[m.group(2)[:3].lower()], int(m.group(3)) if m.group(3) else None)
    for m in _MDY.finditer(text):
        add(int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], int(m.group(3)) if m.group(3) else None)
    for m in _NUM.finditer(text):
        add(int(m.group(1)), int(m.group(2)), int(m.group(3)))  # Australian day-first
    return found


def date_supported(iso: str | None, evidence: str | None, corpus: str) -> tuple[date | None, bool]:
    """-> (date or None, year_inferred). Accepts the date only if the printed text contains it."""
    if not iso:
        return None, False
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None, False
    ev_on_page = bool(evidence) and quote_in_text(evidence, norm(corpus))
    for source in (evidence if ev_on_page else "", corpus):  # a quote that is not actually on the page cannot vouch for a date
        for found, stated in find_dates(source or "", default_year=d.year):
            if found == d:
                return d, not stated
    return None, False


def verify_offer(o: ExtractedOffer, corpus: str) -> tuple[ExtractedOffer, list[str]]:
    """Return a cleaned copy plus human-readable notes about anything removed."""
    cn = norm(corpus)
    notes: list[str] = []
    out = o.model_copy(deep=True)

    good: list[Benefit] = []
    for b in out.benefits:
        if not quote_in_text(b.evidence, cn):
            notes.append(f"benefit '{b.label}' dropped: evidence quote not found in source text")
            continue
        if not amount_supported(b.amount, b.evidence):
            notes.append(f"amount {b.amount:,.0f} for '{b.label}' not in its quote; set to unknown")
            b.amount, b.amount_qualifier = None, None
            if b.amount_kind not in (None, "unpriced"):
                b.amount_kind = "unpriced"
        for fld in ("customer_payment_required", "builder_contribution"):
            v = getattr(b, fld)
            if v is not None and not amount_supported(v, b.evidence):
                notes.append(f"{fld} {v:,.0f} for '{b.label}' not in its quote; set to unknown")
                setattr(b, fld, None)
        if b.amount is None:
            b.amount_qualifier = None
        # an inclusion value is never a cash saving, a payment arrangement never a discount, a grant never builder funded
        if b.mechanism_type == "government_grant":
            b.government_component, b.amount_kind = True, "government_grant"
        if b.mechanism_type == "upgrade_inclusion_package" and b.amount_kind == "cash_saving":
            b.amount_kind = "inclusion_value"
        if b.mechanism_type in ("deposit_assistance", "payment_arrangement") and b.amount_kind == "cash_saving":
            b.amount_kind = "payment_arrangement"
        if b.mechanism_type != "build_time_promise":
            b.time_promise_type, b.time_promise_days = None, None
        good.append(b)
    out.benefits = good

    geos: list[GeoScope] = []
    for g in out.geography:
        if quote_in_text(g.evidence, cn) or norm(g.as_stated) in cn:
            geos.append(g)
        else:
            notes.append(f"geography '{g.as_stated}' dropped: not found in source text")
    out.geography = geos

    e: Eligibility = out.eligibility
    ev_ok = [q for q in e.evidence if quote_in_text(q, cn)]
    if e.evidence and len(ev_ok) < len(e.evidence):
        notes.append("some eligibility evidence quotes were not found in the source text")

    def supported(item: str) -> bool:
        ni = norm(item)
        return ni in cn or any(ni and ni in norm(q) for q in ev_ok) or quote_in_text(item, cn)

    for fld in ("ranges", "designs", "buyer_types", "finance_requirements", "exclusions"):
        items = getattr(e, fld)
        kept = [i for i in items if supported(i)]
        if len(kept) != len(items):
            notes.append(f"{fld}: {len(items) - len(kept)} item(s) without supporting text removed")
        setattr(e, fld, kept)
    for fld in ("contract_condition", "land_status"):
        v = getattr(e, fld)
        if v and not supported(v):
            notes.append(f"{fld} not supported by text; set to unknown")
            setattr(e, fld, None)
    e.evidence = ev_ok

    d: OfferDates = out.dates
    for fld in ("start_date", "expiry_date"):
        iso = getattr(d, fld)
        if iso:
            ok, inferred = date_supported(iso, d.evidence, corpus)
            if ok is None:
                notes.append(f"{fld} {iso} not found in source text; set to unknown")
                setattr(d, fld, None)
            elif fld == "expiry_date":
                d.year_inferred = inferred
    if d.expiry_text and not quote_in_text(d.expiry_text, cn):
        d.expiry_text = None
    if d.expiry_timezone and not re.search(r"\b(aest|aedt|acst|acdt|awst|aet|act|sydney|melbourne|brisbane|adelaide)\b", corpus, re.I):
        notes.append("timezone not stated in source; will be treated as assumed")
        d.expiry_timezone = None

    if out.withdrawn_explicit and not quote_in_text(out.withdrawn_evidence, cn):
        notes.append("withdrawn flag dropped: no supporting text")
        out.withdrawn_explicit, out.withdrawn_evidence = False, None
    out.unresolved = list(dict.fromkeys(out.unresolved + [n for n in notes if "set to unknown" in n or "removed" in n]))
    return out, notes


def verify_page(page: PageExtraction, corpus: str) -> tuple[PageExtraction, list[str]]:
    all_notes, kept = [], []
    for o in page.offers:
        v, notes = verify_offer(o, corpus)
        all_notes.extend(f"[{o.title[:40]}] {n}" for n in notes)
        if v.benefits or v.withdrawn_explicit:  # a date on its own is not an offer
            kept.append(v)
        else:
            all_notes.append(f"[{o.title[:40]}] dropped: no verifiable benefit")
    return PageExtraction(offers=kept, page_has_offers=page.page_has_offers, notes=page.notes), all_notes
