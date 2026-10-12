"""Persistent schema.

Design rules that matter (see brief s12):
* Raw evidence (source_snapshot, trend_request.raw_response, gsc_*_row) is kept apart from
  normalised interpretation (offer_version.fields, offer_mechanism, trend_metric ...).
* Nothing here is deleted by a refresh. Human corrections live in offer_correction and are
  re-applied over every new machine extraction.
* GSC query rows and page rows are separate tables with no join key between them.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

TS = DateTime(timezone=True)
BigPK = BigInteger().with_variant(Integer, "sqlite")


def pk() -> Mapped[int]:
    return mapped_column(BigPK, primary_key=True, autoincrement=True)


# --------------------------------------------------------------------------- identity / geography
class Brand(Base):
    __tablename__ = "brand"
    id: Mapped[int] = pk()
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    is_own: Mapped[bool] = mapped_column(Boolean, default=False)  # Metricon
    official_domains: Mapped[list] = mapped_column(JSONB, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    aliases: Mapped[list["BrandAlias"]] = relationship(back_populates="brand", cascade="all, delete-orphan")
    regions: Mapped[list["BrandRegion"]] = relationship(back_populates="brand", cascade="all, delete-orphan")


class BrandAlias(Base):
    __tablename__ = "brand_alias"
    __table_args__ = (UniqueConstraint("brand_id", "alias"),)
    id: Mapped[int] = pk()
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(160))
    # search_term: used in Trends requests; name_variant: used for text matching only
    kind: Mapped[str] = mapped_column(String(20), default="name_variant")
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    brand: Mapped[Brand] = relationship(back_populates="aliases")


class BrandRegion(Base):
    """A brand's presence in a monitored state. 22 launch rows."""

    __tablename__ = "brand_region"
    __table_args__ = (UniqueConstraint("brand_id", "state"),)
    id: Mapped[int] = pk()
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(3))  # VIC NSW QLD SA
    main_city: Mapped[str | None] = mapped_column(String(60))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    brand: Mapped[Brand] = relationship(back_populates="regions")


# --------------------------------------------------------------------------- sources / evidence
class Source(Base):
    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("url", "context_key"),)
    id: Mapped[int] = pk()
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(30))  # offer_listing offer_detail terms pdf design package display campaign
    url: Mapped[str] = mapped_column(Text)
    context_key: Mapped[str] = mapped_column(String(120), default="")  # regional context e.g. "state=VIC"
    context: Mapped[dict] = mapped_column(JSONB, default=dict)  # cookies/headers/regional selection used
    state: Mapped[str | None] = mapped_column(String(3))  # state this source speaks for, when explicit
    fetch_mode: Mapped[str] = mapped_column(String(10), default="http")  # http | browser
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_from_id: Mapped[int | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    last_success_at: Mapped[datetime | None] = mapped_column(TS)
    last_failure_at: Mapped[datetime | None] = mapped_column(TS)
    last_outcome: Mapped[str | None] = mapped_column(String(30))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    brand: Mapped[Brand | None] = relationship()


class SourceSnapshot(Base):
    """One row per fetch attempt. Bodies are stored once; an unchanged re-fetch references the
    snapshot that holds the body (unchanged_of_id). Rows are never updated after insert."""

    __tablename__ = "source_snapshot"
    __table_args__ = (Index("ix_snapshot_source_time", "source_id", "fetched_at"),)
    id: Mapped[int] = pk()
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    requested_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list] = mapped_column(JSONB, default=list)
    http_status: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(30))  # ok http_error blocked timeout network too_large empty
    error: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    fetch_mode: Mapped[str] = mapped_column(String(10), default="http")
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))  # hash of denoised text
    clean_text: Mapped[str | None] = mapped_column(Text)  # denoised evidence text
    title: Mapped[str | None] = mapped_column(Text)
    links: Mapped[list | None] = mapped_column(JSONB)  # [{href,text}] for discovery
    raw_gz: Mapped[bytes | None] = mapped_column(LargeBinary)
    unchanged_of_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id", ondelete="SET NULL"))
    is_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_notes: Mapped[dict | None] = mapped_column(JSONB)


# --------------------------------------------------------------------------- offers
class Offer(Base):
    __tablename__ = "offer"
    __table_args__ = (
        Index("ix_offer_brand_status", "brand_id", "status"),
        UniqueConstraint("brand_id", "campaign_key"),
    )
    id: Mapped[int] = pk()
    campaign_id: Mapped[str] = mapped_column(String(40), unique=True)  # stable public id e.g. OFR-xxxxxxxx
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"))
    campaign_key: Mapped[str] = mapped_column(String(200))  # normalised identity used for dedupe
    title: Mapped[str] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    terms_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="advertised")
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_set_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    conflicts: Mapped[list] = mapped_column(JSONB, default=list)
    published_start: Mapped[date | None] = mapped_column(Date)
    published_expiry: Mapped[date | None] = mapped_column(Date)
    expiry_timezone: Mapped[str | None] = mapped_column(String(60))
    expiry_text: Mapped[str | None] = mapped_column(Text)
    first_observed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_observed_at: Mapped[datetime | None] = mapped_column(TS)  # last time a successful crawl still showed it
    last_success_check_at: Mapped[datetime | None] = mapped_column(TS)
    last_failed_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    last_failure_reason: Mapped[str | None] = mapped_column(Text)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=True)  # first capture of its source = baseline
    current_version_id: Mapped[int | None] = mapped_column(BigInteger)
    effective: Mapped[dict] = mapped_column(JSONB, default=dict)  # machine fields + corrections, as served to readers
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    brand: Mapped[Brand] = relationship()
    versions: Mapped[list["OfferVersion"]] = relationship(back_populates="offer", order_by="OfferVersion.version_no")
    mechanisms: Mapped[list["OfferMechanism"]] = relationship(back_populates="offer", cascade="all, delete-orphan")
    applicability: Mapped[list["OfferApplicability"]] = relationship(back_populates="offer", cascade="all, delete-orphan")
    sources: Mapped[list["OfferSource"]] = relationship(back_populates="offer", cascade="all, delete-orphan")


class OfferSource(Base):
    """The pages (regional URLs, terms, PDFs) a campaign was seen on. One campaign, many pages."""

    __tablename__ = "offer_source"
    __table_args__ = (UniqueConstraint("offer_id", "source_id"),)
    id: Mapped[int] = pk()
    offer_id: Mapped[int] = mapped_column(ForeignKey("offer.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), default="listing")  # listing detail terms pdf
    first_seen_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id", ondelete="SET NULL"))
    last_seen_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id", ondelete="SET NULL"))
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    geo: Mapped[list] = mapped_column(JSONB, default=list)  # geography implied by this page's regional context
    offer: Mapped[Offer] = relationship(back_populates="sources")
    source: Mapped[Source] = relationship()


class OfferVersion(Base):
    __tablename__ = "offer_version"
    __table_args__ = (UniqueConstraint("offer_id", "version_no"),)
    id: Mapped[int] = pk()
    offer_id: Mapped[int] = mapped_column(ForeignKey("offer.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    fields_hash: Mapped[str] = mapped_column(String(64))
    fields: Mapped[dict] = mapped_column(JSONB)  # validated OfferFields payload (machine extraction)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)  # [{field, excerpt, snapshot_id, source_url}]
    unresolved: Mapped[list] = mapped_column(JSONB, default=list)
    diff: Mapped[list] = mapped_column(JSONB, default=list)  # field-level changes vs previous version
    snapshot_ids: Mapped[list] = mapped_column(JSONB, default=list)
    extraction_method: Mapped[str] = mapped_column(String(12))  # llm | rules
    extraction_version: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    offer: Mapped[Offer] = relationship(back_populates="versions")


class OfferMechanism(Base):
    """Derived from the effective fields; one row per advertised mechanism."""

    __tablename__ = "offer_mechanism"
    id: Mapped[int] = pk()
    offer_id: Mapped[int] = mapped_column(ForeignKey("offer.id", ondelete="CASCADE"))
    mechanism_type: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_qualifier: Mapped[str | None] = mapped_column(String(10))  # exact up_to from
    amount_kind: Mapped[str | None] = mapped_column(String(24))  # cash_saving inclusion_value payment_arrangement grant
    customer_payment_required: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    builder_contribution: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    government_component: Mapped[bool] = mapped_column(Boolean, default=False)
    currency: Mapped[str] = mapped_column(String(3), default="AUD")
    tax_basis: Mapped[str | None] = mapped_column(String(30))
    time_promise_type: Mapped[str | None] = mapped_column(String(24))  # start_of_construction | completion
    time_promise_days: Mapped[int | None] = mapped_column(Integer)
    excerpt: Mapped[str | None] = mapped_column(Text)
    offer: Mapped[Offer] = relationship(back_populates="mechanisms")


class OfferApplicability(Base):
    __tablename__ = "offer_applicability"
    __table_args__ = (Index("ix_offer_app_state", "state", "scope_level"),)
    id: Mapped[int] = pk()
    offer_id: Mapped[int] = mapped_column(ForeignKey("offer.id", ondelete="CASCADE"))
    scope_level: Mapped[str] = mapped_column(String(16))  # state city metro build_region estate franchise
    state: Mapped[str | None] = mapped_column(String(3))
    name: Mapped[str | None] = mapped_column(String(160))
    as_stated: Mapped[str | None] = mapped_column(Text)
    offer: Mapped[Offer] = relationship(back_populates="applicability")


class OfferCorrection(Base):
    """Human override of one effective field. Machine value at the time is kept for audit."""

    __tablename__ = "offer_correction"
    id: Mapped[int] = pk()
    offer_id: Mapped[int] = mapped_column(ForeignKey("offer.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(80))
    value: Mapped[Any] = mapped_column(JSONB)
    machine_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(TS)


# --------------------------------------------------------------------------- competitor activity
class TrackedItem(Base):
    """A design/package/display/facade seen on an official page, keyed by identity so we can tell
    'new' from 'changed' from 'a different product'."""

    __tablename__ = "tracked_item"
    __table_args__ = (UniqueConstraint("source_id", "item_key"),)
    id: Mapped[int] = pk()
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    item_type: Mapped[str] = mapped_column(String(20))  # design package display facade estate range
    item_key: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(3))
    url: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    first_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    first_seen_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id", ondelete="SET NULL"))
    last_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(TS)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=True)


class CompetitorEvent(Base):
    __tablename__ = "competitor_event"
    __table_args__ = (Index("ix_event_brand_date", "brand_id", "observed_at"),)
    id: Mapped[int] = pk()
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"))
    event_type: Mapped[str] = mapped_column(String(30))
    state: Mapped[str | None] = mapped_column(String(3))
    geography: Mapped[dict] = mapped_column(JSONB, default=dict)
    theme_slug: Mapped[str | None] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    effective_date: Mapped[date | None] = mapped_column(Date)  # only when the source states it
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id", ondelete="SET NULL"))
    source_url: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_before_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id", ondelete="SET NULL"))
    snapshot_after_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshot.id", ondelete="SET NULL"))
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offer.id", ondelete="SET NULL"))
    brand: Mapped[Brand] = relationship()


# --------------------------------------------------------------------------- themes + GSC
class Theme(Base):
    __tablename__ = "theme"
    id: Mapped[int] = pk()
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("theme.id", ondelete="SET NULL"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_discovery: Mapped[bool] = mapped_column(Boolean, default=False)  # candidate rather than confirmed

    rules: Mapped[list["ThemeRule"]] = relationship(back_populates="theme", cascade="all, delete-orphan")
    keywords: Mapped[list["ThemeKeyword"]] = relationship(back_populates="theme", cascade="all, delete-orphan")


class ThemeRule(Base):
    """Cheap deterministic mapping from GSC query text to a theme."""

    __tablename__ = "theme_rule"
    id: Mapped[int] = pk()
    theme_id: Mapped[int] = mapped_column(ForeignKey("theme.id", ondelete="CASCADE"))
    pattern: Mapped[str] = mapped_column(Text)
    match_type: Mapped[str] = mapped_column(String(10), default="regex")  # regex | phrase
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower wins
    theme: Mapped[Theme] = relationship(back_populates="rules")


class ThemeKeyword(Base):
    """Representative terms sent to Trends. A small, editable set per theme."""

    __tablename__ = "theme_keyword"
    __table_args__ = (UniqueConstraint("theme_id", "keyword"),)
    id: Mapped[int] = pk()
    theme_id: Mapped[int] = mapped_column(ForeignKey("theme.id", ondelete="CASCADE"))
    keyword: Mapped[str] = mapped_column(String(160))
    subtheme: Mapped[str | None] = mapped_column(String(120))
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="gsc")  # gsc | seed | discovery | admin
    gsc_impressions: Mapped[int | None] = mapped_column(BigInteger)
    theme: Mapped[Theme] = relationship(back_populates="keywords")


class QueryMappingOverride(Base):
    """Admin decision on a query's theme; applied to every import (current and future)."""

    __tablename__ = "query_mapping_override"
    id: Mapped[int] = pk()
    query_norm: Mapped[str] = mapped_column(String(400), unique=True)
    theme_id: Mapped[int | None] = mapped_column(ForeignKey("theme.id", ondelete="CASCADE"))  # null => excluded/off-topic
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class GscImport(Base):
    __tablename__ = "gsc_import"
    id: Mapped[int] = pk()
    file_name: Mapped[str] = mapped_column(String(300))
    file_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)  # hash of canonical rows: same data => same import
    reported_period_start: Mapped[date | None] = mapped_column(Date)
    reported_period_end: Mapped[date | None] = mapped_column(Date)
    period_note: Mapped[str | None] = mapped_column(Text)  # reported by user; not a column in the data
    query_row_count: Mapped[int] = mapped_column(Integer, default=0)
    page_row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(12), default="complete")


class GscQueryRow(Base):
    __tablename__ = "gsc_query_row"
    __table_args__ = (UniqueConstraint("import_id", "query"), Index("ix_gscq_theme", "import_id", "theme_id"))
    id: Mapped[int] = pk()
    import_id: Mapped[int] = mapped_column(ForeignKey("gsc_import.id", ondelete="CASCADE"))
    source_row: Mapped[int] = mapped_column(Integer)  # 1-based row number in the sheet (header = 1)
    query: Mapped[str] = mapped_column(Text)  # original text, preserved
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    ctr: Mapped[float] = mapped_column(Float)
    position: Mapped[float] = mapped_column(Float)
    query_norm: Mapped[str] = mapped_column(String(400))
    theme_id: Mapped[int | None] = mapped_column(ForeignKey("theme.id", ondelete="SET NULL"))
    mapping_method: Mapped[str | None] = mapped_column(String(12))  # rule | llm | admin | none
    state_hint: Mapped[str | None] = mapped_column(String(3))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)  # off-topic (rentals, decorating ...)
    has_brand_term: Mapped[bool] = mapped_column(Boolean, default=False)


class GscPageRow(Base):
    __tablename__ = "gsc_page_row"
    __table_args__ = (UniqueConstraint("import_id", "page"),)
    id: Mapped[int] = pk()
    import_id: Mapped[int] = mapped_column(ForeignKey("gsc_import.id", ondelete="CASCADE"))
    source_row: Mapped[int] = mapped_column(Integer)
    page: Mapped[str] = mapped_column(Text)
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    ctr: Mapped[float] = mapped_column(Float)
    position: Mapped[float] = mapped_column(Float)
    path: Mapped[str] = mapped_column(Text)
    page_kind: Mapped[str | None] = mapped_column(String(40))  # house_and_land display_homes designs knockdown_rebuild ...
    theme_slug: Mapped[str | None] = mapped_column(String(60))
    state_hint: Mapped[str | None] = mapped_column(String(3))


# --------------------------------------------------------------------------- trends
class TrendLocation(Base):
    __tablename__ = "trend_location"
    __table_args__ = (UniqueConstraint("provider", "location_code"),)
    id: Mapped[int] = pk()
    provider: Mapped[str] = mapped_column(String(30), default="dataforseo_google_trends")
    location_code: Mapped[int] = mapped_column(Integer)
    location_name: Mapped[str] = mapped_column(String(200))
    location_type: Mapped[str | None] = mapped_column(String(40))
    parent_code: Mapped[int | None] = mapped_column(Integer)
    country_iso: Mapped[str | None] = mapped_column(String(4))
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class TrendCapability(Base):
    """Recorded outcome of a real provider test for one geography."""

    __tablename__ = "trend_capability"
    __table_args__ = (UniqueConstraint("geo_key"),)
    id: Mapped[int] = pk()
    geo_key: Mapped[str] = mapped_column(String(40))  # AU, VIC, NSW, QLD, SA, city:Melbourne ...
    geo_level: Mapped[str] = mapped_column(String(10))  # country state city
    location_code: Mapped[int | None] = mapped_column(Integer)
    location_name: Mapped[str | None] = mapped_column(String(200))
    supported: Mapped[bool | None] = mapped_column(Boolean)  # null = untested
    coverage: Mapped[str | None] = mapped_column(String(30))  # full sparse none
    tested_at: Mapped[datetime | None] = mapped_column(TS)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class TrendPlan(Base):
    """What to refresh weekly: a comparable group of <=5 keywords for one geography."""

    __tablename__ = "trend_plan"
    __table_args__ = (UniqueConstraint("plan_key"),)
    id: Mapped[int] = pk()
    plan_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(12))  # compare | discover
    purpose: Mapped[str] = mapped_column(String(20))  # brands theme
    geo_key: Mapped[str] = mapped_column(String(40))
    keywords: Mapped[list] = mapped_column(JSONB)
    theme_slug: Mapped[str | None] = mapped_column(String(60))
    brand_ids: Mapped[list] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    pilot: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class TrendRequest(Base):
    __tablename__ = "trend_request"
    id: Mapped[int] = pk()
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # compare | discover
    provider: Mapped[str] = mapped_column(String(40), default="dataforseo_google_trends")
    keywords: Mapped[list] = mapped_column(JSONB)
    geo_key: Mapped[str] = mapped_column(String(40))
    geo_level: Mapped[str] = mapped_column(String(10))
    location_code: Mapped[int | None] = mapped_column(Integer)
    location_name: Mapped[str | None] = mapped_column(String(200))
    search_type: Mapped[str] = mapped_column(String(20), default="web")
    category_code: Mapped[int] = mapped_column(Integer, default=0)
    date_from: Mapped[date | None] = mapped_column(Date)
    date_to: Mapped[date | None] = mapped_column(Date)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="ok")  # ok failed
    error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    cost_reported: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    cost_estimated: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    raw_response: Mapped[dict | None] = mapped_column(JSONB)
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)
    purpose: Mapped[str | None] = mapped_column(String(20))
    theme_slug: Mapped[str | None] = mapped_column(String(60))


class TrendObservation(Base):
    __tablename__ = "trend_observation"
    __table_args__ = (UniqueConstraint("request_id", "keyword", "period_start"), Index("ix_trend_obs_req", "request_id"))
    id: Mapped[int] = pk()
    request_id: Mapped[int] = mapped_column(ForeignKey("trend_request.id", ondelete="CASCADE"))
    keyword: Mapped[str] = mapped_column(String(160))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    value: Mapped[int | None] = mapped_column(Integer)  # NULL = missing (never coerced to 0)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)


class TrendMetric(Base):
    __tablename__ = "trend_metric"
    __table_args__ = (UniqueConstraint("request_id", "keyword", "metric_key"),)
    id: Mapped[int] = pk()
    request_id: Mapped[int] = mapped_column(ForeignKey("trend_request.id", ondelete="CASCADE"))
    keyword: Mapped[str] = mapped_column(String(160))
    metric_key: Mapped[str] = mapped_column(String(20))  # mom4 yoy4
    status: Mapped[str] = mapped_column(String(12))  # ok unavailable
    value: Mapped[float | None] = mapped_column(Float)  # percentage change
    current_avg: Mapped[float | None] = mapped_column(Float)
    comparison_avg: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class RelatedQueryCandidate(Base):
    __tablename__ = "related_query_candidate"
    __table_args__ = (UniqueConstraint("seed", "geo_key", "query"),)
    id: Mapped[int] = pk()
    request_id: Mapped[int | None] = mapped_column(ForeignKey("trend_request.id", ondelete="SET NULL"))
    seed: Mapped[str] = mapped_column(String(160))
    geo_key: Mapped[str] = mapped_column(String(40))
    query: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))  # rising top
    value: Mapped[int | None] = mapped_column(Integer)
    relevant: Mapped[bool | None] = mapped_column(Boolean)  # null = unreviewed
    review_status: Mapped[str] = mapped_column(String(12), default="proposed")  # proposed accepted rejected
    suggested_theme_slug: Mapped[str | None] = mapped_column(String(60))
    validated_trend: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


# --------------------------------------------------------------------------- macro
class MacroSeries(Base):
    __tablename__ = "macro_series"
    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(80), unique=True)
    indicator: Mapped[str] = mapped_column(String(40))  # house_approvals house_output_ppi cash_rate
    name: Mapped[str] = mapped_column(String(200))
    geography: Mapped[str] = mapped_column(String(20))  # AU VIC NSW QLD SA
    source: Mapped[str] = mapped_column(String(10))  # ABS RBA
    dataset: Mapped[str | None] = mapped_column(String(60))
    series_key: Mapped[str | None] = mapped_column(String(200))
    series_id: Mapped[str | None] = mapped_column(String(60))  # ABS catalogue-style series id when published
    unit: Mapped[str] = mapped_column(String(40))
    frequency: Mapped[str] = mapped_column(String(12))
    adjustment: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)  # the actual metric, shown in a tooltip
    source_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    coverage_note: Mapped[str | None] = mapped_column(Text)


class MacroObservation(Base):
    __tablename__ = "macro_observation"
    __table_args__ = (UniqueConstraint("series_id_fk", "period"),)
    id: Mapped[int] = pk()
    series_id_fk: Mapped[int] = mapped_column(ForeignKey("macro_series.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(12))  # 2026-08, 2026-Q2, 2026-10-07
    period_end: Mapped[date | None] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    release_id: Mapped[int | None] = mapped_column(ForeignKey("macro_release.id", ondelete="SET NULL"))
    first_retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)


class MacroRevision(Base):
    __tablename__ = "macro_revision"
    id: Mapped[int] = pk()
    observation_id: Mapped[int] = mapped_column(ForeignKey("macro_observation.id", ondelete="CASCADE"))
    previous_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    new_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    release_id: Mapped[int | None] = mapped_column(ForeignKey("macro_release.id", ondelete="SET NULL"))
    detected_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class MacroRelease(Base):
    __tablename__ = "macro_release"
    __table_args__ = (UniqueConstraint("source", "dataset", "release_key"),)
    id: Mapped[int] = pk()
    source: Mapped[str] = mapped_column(String(10))
    dataset: Mapped[str] = mapped_column(String(60))
    release_key: Mapped[str] = mapped_column(String(80))  # reference period / etag of the latest release
    reference_period: Mapped[str | None] = mapped_column(String(40))
    publication_date: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    url: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


# --------------------------------------------------------------------------- news
class NewsQuery(Base):
    __tablename__ = "news_query"
    __table_args__ = (UniqueConstraint("text", "state"),)
    id: Mapped[int] = pk()
    text: Mapped[str] = mapped_column(String(300))
    state: Mapped[str | None] = mapped_column(String(3))
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id", ondelete="SET NULL"))
    theme_slug: Mapped[str | None] = mapped_column(String(60))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class NewsItem(Base):
    __tablename__ = "news_item"
    id: Mapped[int] = pk()
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    title_key: Mapped[str] = mapped_column(String(200), index=True)  # syndication dedupe
    publisher: Mapped[str | None] = mapped_column(String(200))
    publisher_domain: Mapped[str | None] = mapped_column(String(200))
    source_class: Mapped[str] = mapped_column(String(20), default="third_party")  # official_stats builder_claim government third_party
    published_at: Mapped[datetime | None] = mapped_column(TS)
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    event_date: Mapped[date | None] = mapped_column(Date)
    snippet: Mapped[str | None] = mapped_column(Text)
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    evidence_level: Mapped[str] = mapped_column(String(14), default="headline_only")  # headline_only | article
    resolved_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(3))
    brand_ids: Mapped[list] = mapped_column(JSONB, default=list)
    theme_slug: Mapped[str | None] = mapped_column(String(60))
    relevance: Mapped[str] = mapped_column(String(10), default="kept")  # kept excluded
    relevance_reason: Mapped[str | None] = mapped_column(String(200))
    query_ids: Mapped[list] = mapped_column(JSONB, default=list)
    also_reported_by: Mapped[list] = mapped_column(JSONB, default=list)


# --------------------------------------------------------------------------- briefs
class Brief(Base):
    __tablename__ = "brief"
    __table_args__ = (Index("ix_brief_scope_week", "scope", "week_start"),)
    id: Mapped[int] = pk()
    scope: Mapped[str] = mapped_column(String(3))  # AU VIC NSW QLD SA
    week_start: Mapped[date] = mapped_column(Date)  # Monday, Australia/Sydney
    status: Mapped[str] = mapped_column(String(12))  # ok failed unavailable
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    model: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(30))
    input_facts: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    summary_note: Mapped[str | None] = mapped_column(Text)
    highlights: Mapped[list["BriefHighlight"]] = relationship(
        back_populates="brief", cascade="all, delete-orphan", order_by="BriefHighlight.position"
    )


class BriefHighlight(Base):
    __tablename__ = "brief_highlight"
    id: Mapped[int] = pk()
    brief_id: Mapped[int] = mapped_column(ForeignKey("brief.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    headline: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict] = mapped_column(JSONB)  # {geography, brands, themes}
    observation: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list] = mapped_column(JSONB)  # [{type,id,label,url}]
    interpretation: Mapped[str] = mapped_column(Text)
    marketing_implication: Mapped[str] = mapped_column(Text)
    sales_implication: Mapped[str] = mapped_column(Text)
    evidence_limitation: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[Brief] = relationship(back_populates="highlights")


# --------------------------------------------------------------------------- access / ops
class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = pk()
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="viewer")  # viewer | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TS)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = pk()
    at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    before: Mapped[Any] = mapped_column(JSONB, nullable=True)
    after: Mapped[Any] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text)


class JobRun(Base):
    __tablename__ = "job_run"
    __table_args__ = (Index("ix_job_name_started", "job_name", "started_at"),)
    id: Mapped[int] = pk()
    job_name: Mapped[str] = mapped_column(String(60))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued running succeeded partial failed skipped
    trigger: Mapped[str] = mapped_column(String(12), default="schedule")  # schedule manual retry
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    queued_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class SpendLedger(Base):
    __tablename__ = "spend_ledger"
    id: Mapped[int] = pk()
    at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    provider: Mapped[str] = mapped_column(String(30))
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))  # reported | estimated
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 5), default=0)
    calls: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class AppSetting(Base):
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
