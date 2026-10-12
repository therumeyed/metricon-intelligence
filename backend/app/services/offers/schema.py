"""Typed contract for offer extraction (LLM or rules). Every money amount, date and eligibility rule carries a
verbatim `evidence` quote that is checked against the fetched text before it is accepted (see verify.py)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

MechanismType = Literal[
    "cash_discount", "upgrade_inclusion_package", "deposit_assistance", "payment_arrangement", "price_hold",
    "site_costs", "facade_benefit", "build_time_promise", "rebate", "government_grant", "finance_incentive", "other",
]
AmountKind = Literal["cash_saving", "inclusion_value", "payment_arrangement", "government_grant", "unpriced"]
GeoLevel = Literal["state", "city", "metro", "build_region", "estate", "franchise"]
State = Literal["VIC", "NSW", "QLD", "SA", "WA", "TAS", "NT", "ACT"]

MECHANISM_LABELS = {
    "cash_discount": "Cash discount", "upgrade_inclusion_package": "Upgrades / inclusions", "deposit_assistance": "Deposit assistance",
    "payment_arrangement": "Payment arrangement", "price_hold": "Price hold / lock", "site_costs": "Site costs", "facade_benefit": "Facade benefit",
    "build_time_promise": "Build-time promise", "rebate": "Rebate", "government_grant": "Government grant", "finance_incentive": "Finance incentive", "other": "Other",
}


class Benefit(BaseModel):
    mechanism_type: MechanismType
    label: str = Field(description="Short plain description, e.g. 'Free site costs' or '12 month price lock'")
    amount: Optional[float] = Field(None, description="Advertised dollar figure exactly as printed (e.g. $82K -> 82000). null if no figure is printed. Never estimate.")
    amount_qualifier: Optional[Literal["exact", "up_to", "from"]] = Field(None, description="'up_to' for 'up to', 'from' for 'from', else 'exact'")
    amount_kind: Optional[AmountKind] = Field(None, description="cash_saving = money off the contract price; inclusion_value = claimed value of upgrades/inclusions (NOT a saving); payment_arrangement = deposit/payment timing (NOT a discount); government_grant = paid by government (NOT builder funded); unpriced = no figure")
    customer_payment_required: Optional[float] = Field(None, description="Amount the customer must pay, e.g. 'only $2,000 deposit'. null if not stated.")
    builder_contribution: Optional[float] = Field(None, description="Amount the builder explicitly contributes, if stated separately")
    government_component: bool = False
    tax_basis: Optional[Literal["incl_gst", "excl_gst", "not_stated"]] = None
    time_promise_type: Optional[Literal["start_of_construction", "completion"]] = Field(None, description="Keep 'time until construction starts' separate from 'time to complete the build'")
    time_promise_days: Optional[int] = None
    evidence: str = Field(description="Verbatim quote (max 300 chars) from the document supporting this benefit and its figures")


class GeoScope(BaseModel):
    level: GeoLevel
    state: Optional[State] = None
    name: Optional[str] = Field(None, description="e.g. 'Southern NSW', 'Melbourne', 'Gold Coast', an estate name")
    as_stated: str = Field(description="The wording used in the document")
    evidence: str = Field(description="Verbatim quote")


class Eligibility(BaseModel):
    ranges: list[str] = Field(default_factory=list, description="Design ranges/collections the offer applies to")
    designs: list[str] = Field(default_factory=list, description="Specific designs if named")
    contract_condition: Optional[str] = Field(None, description="e.g. 'new deposits taken between X and Y', 'contract signed by ...'")
    buyer_types: list[str] = Field(default_factory=list, description="e.g. first home buyers, investors, owner-occupiers")
    land_status: Optional[str] = Field(None, description="e.g. 'titled land only', 'land must be registered by...'")
    finance_requirements: list[str] = Field(default_factory=list, description="e.g. 'must finance through partner lender'")
    exclusions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, description="Verbatim quotes supporting the items above")


class OfferDates(BaseModel):
    start_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD only if a start date is printed")
    expiry_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD only if an end date is printed. 'Limited time' is NOT a date.")
    expiry_timezone: Optional[str] = Field(None, description="IANA timezone only if the source states one")
    expiry_text: Optional[str] = Field(None, description="The expiry wording as printed")
    year_inferred: bool = False
    evidence: Optional[str] = None


class ExtractedOffer(BaseModel):
    title: str
    headline: Optional[str] = None
    benefits: list[Benefit] = Field(default_factory=list)
    geography: list[GeoScope] = Field(default_factory=list, description="Only regions the document explicitly names. Do not infer from the website's country or the brand's footprint.")
    eligibility: Eligibility = Field(default_factory=Eligibility)
    dates: OfferDates = Field(default_factory=OfferDates)
    detail_url: Optional[str] = Field(None, description="Link to this offer's own page if the listing links to one")
    terms_url: Optional[str] = Field(None, description="Link to terms/PDF if one is referenced")
    withdrawn_explicit: bool = False
    withdrawn_evidence: Optional[str] = None
    conflicts: list[str] = Field(default_factory=list, description="Contradictions between landing text and terms, if both present")
    unresolved: list[str] = Field(default_factory=list, description="Fields you could not determine from the text")


class PageExtraction(BaseModel):
    offers: list[ExtractedOffer] = Field(default_factory=list)
    page_has_offers: bool = True
    notes: Optional[str] = None
