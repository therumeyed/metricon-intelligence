"""Deterministic fallback extractor used when no Anthropic key is configured (or the model call fails).

It is deliberately conservative: it reads promotional headline blocks, pulls dollar figures with their qualifiers,
publishes dates only when printed, and flags everything it cannot read (conditions, eligibility) as unresolved so the
offer lands in the review queue. It never invents geography.
"""
from __future__ import annotations

import re

from .schema import Benefit, ExtractedOffer, Eligibility, OfferDates, PageExtraction
from .verify import find_dates, money_values

HEAD_RX = re.compile(
    r"\$\s?\d|\d\s?%|\bupfront\b|\b(?:price (?:lock|hold|drop|promise)|free|bonus|rebate|discount|save|savings|upgrade|upgrades|inclusions?|giveaway|offer|promotion|"
    r"site costs?|deposit|build time|cash ?back|bundle|incentive|fixed price)\b", re.I)
SKIP_RX = re.compile(r"^(view|find out|learn|read|click|see|enquire|get in touch|contact|book|register|submit|terms and conditions|\*?terms)\b", re.I)
EXPIRY_CUE = re.compile(r"\b(?:ends?|ending|expires?|expiry|until|valid (?:until|to|through)|by|before|closes?)\b", re.I)
QUAL_RX = re.compile(r"\b(up to|from|worth up to|valued? (?:at|up to)|save up to|save|over|more than|only)\b[^$]{0,12}$", re.I)


def _mechanism(text: str) -> tuple[str, str | None]:
    t = text.lower()
    if re.search(r"price (lock|hold|promise)|fixed price|lock in a (fixed )?price", t):
        return "price_hold", "payment_arrangement" if False else None
    if re.search(r"site costs?", t):
        return "site_costs", None
    if re.search(r"facade|façade", t):
        return "facade_benefit", None
    if re.search(r"\b(first home|grant)\b", t) and re.search(r"grant|fhog", t):
        return "government_grant", "government_grant"
    if re.search(r"\b(\d+ ?(?:weeks?|days?|months?)|underway|start(?:ed)?|build time|turbo|fast ?track)\b", t) and re.search(r"build time|underway|start|complete|handover|weeks|days", t):
        return "build_time_promise", None
    if re.search(r"deposit|until slab|pay (?:only|just)|upfront|payable at completion|\bpay\b.*\bcompletion", t):
        return "deposit_assistance", "payment_arrangement"
    if re.search(r"upgrade|inclusions?|giveaway|bundle|luxury|appliance|solar|landscap|flooring", t):
        return "upgrade_inclusion_package", "inclusion_value"
    if re.search(r"rebate|cash ?back", t):
        return "rebate", "cash_saving"
    if re.search(r"\$\s?[\d,]+[kK]?\s*off|discount|price drop|save\b|savings", t):
        return "cash_discount", "cash_saving"
    return "other", None


def _time_promise(text: str) -> tuple[str | None, int | None]:
    t = text.lower()
    m = re.search(r"(\d{1,3})\s*(weeks?|days?|months?)", t)
    if not m:
        return None, None
    n = int(m.group(1))
    days = n * (7 if m.group(2).startswith("week") else 30 if m.group(2).startswith("month") else 1)
    if re.search(r"underway|start|commence|site start|from deposit|until construction", t):
        return "start_of_construction", days
    if re.search(r"build time|complet|handover|finish|move[- ]in", t):
        return "completion", days
    return None, days


def _first_amount(text: str) -> tuple[float | None, str | None]:
    """First $ figure with its 'up to'/'from' qualifier taken from the words just before it."""
    m = re.search(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kKmM]?\b", text)
    if not m:
        return None, None
    vals = money_values(m.group(0))
    if not vals:
        return None, None
    before = text[max(0, m.start() - 24):m.start()].lower()
    q = "up_to" if "up to" in before else "from" if re.search(r"\bfrom\s*$", before) else "exact"
    return vals[0], q


GENERIC_TITLE = re.compile(r"^(limited time( offer)?|don.t miss out|claim your offer|even more ways|more ways to save|view (all )?offers?|current offers?|our offers?|special offers?|exclusive offers?|promotions?|offers?)\b[\s.!]*$|^don.t miss out", re.I)


def _merge_orphan_amounts(blocks: list[str]) -> list[str]:
    """A bare figure such as '$40,000' followed by its label becomes '$40,000 Design Bonus'."""
    out, i = [], 0
    while i < len(blocks):
        b = blocks[i]
        if i + 1 < len(blocks) and len(b) <= 14 and re.fullmatch(r"(up to |from )?\$\s?[\d,]+[kK]?\*?", b.strip(), re.I) and len(blocks[i + 1]) <= 80:
            out.append(f"{b} {blocks[i + 1]}")
            i += 2
            continue
        out.append(b)
        i += 1
    return out


def extract_from_blocks(blocks: list[str], default_year: int | None = None, link_map: dict[str, str] | None = None) -> PageExtraction:
    blocks = _merge_orphan_amounts(blocks)
    heads = [i for i, b in enumerate(blocks) if HEAD_RX.search(b) and 6 <= len(b) <= 140 and not SKIP_RX.match(b) and not GENERIC_TITLE.match(b.strip())]
    offers: list[ExtractedOffer] = []
    used = set()
    for n, i in enumerate(heads):
        if i in used:
            continue
        end = heads[n + 1] if n + 1 < len(heads) else min(len(blocks), i + 5)
        group = blocks[i:min(end, i + 5)]
        for j in range(i, i + len(group)):
            used.add(j)
        text = " ".join(group)
        title = blocks[i].strip(" *^#")
        mech, kind = _mechanism(group[0])  # the headline decides the type; body text only breaks a tie
        if mech == "other":
            mech, kind = _mechanism(text)
        amount, qual = _first_amount(group[0])
        if amount is None and mech not in ("build_time_promise", "price_hold"):
            amount, qual = _first_amount(text)
        tp, days = _time_promise(group[0] if mech == "build_time_promise" else text) if mech == "build_time_promise" else (None, None)
        quote = group[0] if len(group[0]) > 12 else text[:300]
        if amount is not None and not any(abs(v - amount) < .5 for v in money_values(quote)):
            quote = text[:300]
        benefit = Benefit(
            mechanism_type=mech, label=title[:120], amount=amount, amount_qualifier=qual if amount is not None else None,
            amount_kind=kind if amount is not None else ("unpriced" if kind else None),
            customer_payment_required=_customer_pay(text, mech), government_component=(mech == "government_grant"),
            time_promise_type=tp, time_promise_days=days, evidence=quote[:300])
        expiry = None
        ev = None
        for g in group:
            if EXPIRY_CUE.search(g):
                ds = find_dates(g, default_year)
                if ds:
                    expiry, ev = ds[0][0].isoformat(), g[:300]
                    break
        elig = Eligibility()
        if re.search(r"financ|lender|mortgage|loan", text, re.I):
            sent = next((g for g in group if re.search(r"financ|lender|mortgage|loan", g, re.I)), None)
            if sent:
                elig.finance_requirements = [sent[:200]]
                elig.evidence = [sent[:300]]
        offers.append(ExtractedOffer(
            title=title[:160], headline=title[:200], benefits=[benefit],
            eligibility=elig, dates=OfferDates(expiry_date=expiry, evidence=ev, expiry_text=ev),
            detail_url=(link_map or {}).get(title.lower()),
            unresolved=["conditions and eligibility not fully extracted (rules mode)", "geography not stated in text"]))
    return PageExtraction(offers=offers, page_has_offers=bool(offers), notes="rules extractor")


def _customer_pay(text: str, mech: str) -> float | None:
    if mech not in ("deposit_assistance", "payment_arrangement"):
        return None
    m = re.search(r"(?:only|just|with|start with|pay)\s+(?:a\s+)?(\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?)", text, re.I)
    if m:
        v = money_values(m.group(1))
        return v[0] if v else None
    return None
