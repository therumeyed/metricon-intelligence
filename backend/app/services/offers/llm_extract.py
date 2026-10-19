"""Claude-assisted structured extraction. The model only reads text; arithmetic, dates, dedupe and status logic stay in code."""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from ..llm import LLM, wrap_document
from .schema import Eligibility, OfferDates, PageExtraction

PROMPT_VERSION = "offer-extract-2026-10-b"

SYSTEM = """You extract home-builder promotions from public web pages for a market-intelligence tool used by Metricon's marketing and sales teams.

Rules - follow all of them:
1. Report only what the document says. If something is not printed, leave it null / empty. Never estimate, infer or fill gaps.
2. Every benefit, geography and eligibility item needs `evidence`: a short VERBATIM quote copied from the document.
3. Keep benefit types distinct. A claimed value of upgrades/inclusions ("up to $82K in inclusions") is inclusion_value, NOT a cash saving. A deposit or payment-timing arrangement is payment_arrangement, NOT a discount. A government grant is government_grant and is not builder funded. Preserve 'up to' and 'from' as amount_qualifier.
4. Do not assign dollar values to unpriced items (set amount null, amount_kind 'unpriced'). A promise about when construction STARTS (e.g. 'underway in 14 weeks from deposit') is start_of_construction; a promise about how long the BUILD takes is completion. Never merge them.
5. Geography: list only regions the document explicitly names (state, city, metro area, build region such as 'Southern NSW', estate, franchise). Do not infer geography from the website, the brand, or the page URL. An offer for Southern NSW is not a Sydney offer.
6. Dates: give ISO dates only when a date is printed. 'Limited time' or 'while stocks last' is not a date. Put the exact wording in expiry_text. Only give a timezone if the document states one.
7. Eligibility: capture design ranges/designs, contract or deposit conditions, buyer type, land status, finance requirements (e.g. must finance with a named lender), and exclusions.
8. If the landing text and the terms conflict, record the contradiction in `conflicts`.
9. One entry per distinct campaign. The same campaign shown in several places is one entry. Ignore navigation, blog posts, display-home listings and generic marketing copy that is not an offer.
10. If a card links to its own offer page, put that URL (choose from the LINKS list only) in detail_url; the same for terms/PDF links in terms_url.
11. If nothing on the page is a current offer, return an empty offers list. If the page says an offer is withdrawn/ended, set withdrawn_explicit with the quote."""


SERP_NOTE = """

THIS DOCUMENT IS NOT A WEB PAGE. It is the title, result URL and snippet of Google search results taken from the brand's own website for the search "{query}". Snippets are short and often cut off mid-sentence. Extra rules:
- Report an offer only if a result's title or snippet itself states a promotion, benefit, price or incentive. A result that only describes the brand, a design or a display home is not an offer.
- Evidence quotes must be copied from a result's title or snippet.
- Do not assume terms, eligibility, geography or dates that are not printed. Say what is missing in `unresolved`.
- Set detail_url to the "Result URL" of the result the offer came from (choose from the LINKS list).
- The same campaign appearing in several results, or worded slightly differently, is ONE entry.
- Snippets rarely carry full conditions. Do not guess them."""


class TermsEnrichment(BaseModel):
    eligibility: Eligibility = Field(default_factory=Eligibility)
    dates: OfferDates = Field(default_factory=OfferDates)
    conflicts: list[str] = Field(default_factory=list)
    geography_note: Optional[str] = None
    still_unresolved: list[str] = Field(default_factory=list)


def extract_page(llm: LLM, *, brand: str, url: str, text: str, links: list[dict], context: dict, observed: date, serp: str | None = None) -> PageExtraction:
    link_lines = "\n".join(f"- {l['text'][:80]} -> {l['href']}" for l in links[:80])
    user = (
        f"Brand: {brand}\nPage URL: {url}\nObserved on: {observed.isoformat()} (Australia)\n"
        f"Regional context of this page (NOT proof the offer applies there unless the text says so): {context or 'none'}\n\n"
        f"LINKS found on the page:\n{link_lines or '(none)'}\n\n{wrap_document(text, url)}\n\n"
        "Extract every distinct current promotion on this page."
    )
    system = SYSTEM + (SERP_NOTE.format(query=serp) if serp else "")
    return llm.structured(system=system, user=user, schema=PageExtraction, tool_name="submit_offers", max_tokens=16000)


def enrich_from_terms(llm: LLM, *, brand: str, offer_title: str, offer_summary: str, terms_url: str, terms_text: str, observed: date) -> TermsEnrichment:
    user = (
        f"Brand: {brand}\nOffer: {offer_title}\nWhat the landing page says: {offer_summary}\nTerms URL: {terms_url}\nObserved: {observed.isoformat()}\n\n"
        f"{wrap_document(terms_text[:30000], terms_url)}\n\n"
        "From the terms text only, return the conditions that apply specifically to THIS offer: eligibility (ranges, designs, deposit/contract conditions, buyer "
        "type, land status, finance requirements, exclusions) with verbatim evidence quotes, any printed start/expiry dates, and any contradiction with the landing "
        "page summary. If the terms do not mention this offer, return empty lists and say so in still_unresolved."
    )
    return llm.structured(system=SYSTEM, user=user, schema=TermsEnrichment, tool_name="submit_terms", max_tokens=16000)
