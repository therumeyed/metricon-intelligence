"""Behaviour tests for the offers pipeline (brief s15 checks 6-11). Pages and model outputs here are SYNTHETIC test fixtures."""
from datetime import date, datetime, timezone

from sqlalchemy import func, select

from app.models import AuditLog, CompetitorEvent, Offer, OfferSource, Source
from app.services import events, fetcher, queries as Q
from app.services.offers import pipeline, verify
from app.services.offers.schema import Benefit, Eligibility, ExtractedOffer, GeoScope, OfferDates, PageExtraction
from tests.helpers import FakeLLM, fail_result, ok_result, page

URL = "https://www.carlislehomes.com.au/offers/"
MAIN = """<h2>The Better Deal Affinity Range</h2>
<p>Receive up to $82K in luxury inclusions on selected Affinity designs.</p>
<p>Only $2,000 to pay until slab when financed through Partner Lender. Offer ends 30 November 2026.</p>
<p>*Terms and conditions apply. Offer excludes knockdown rebuild.</p>"""


def src(db, url=URL, kind="offer_listing", slug="carlisle-homes", state="VIC", ctx=None) -> Source:
    brand_id = db.scalar(select(Source.brand_id).join(Source.brand.property.mapper.class_, isouter=False).limit(1)) if False else None
    from app.models import Brand

    b = db.scalar(select(Brand).where(Brand.slug == slug))
    s = db.scalar(select(Source).where(Source.url == url))
    if s is None:
        s = Source(brand_id=b.id, kind=kind, url=url, state=state, context=ctx or {}, context_key=(ctx or {}).get("region", ""))
        db.add(s)
        db.flush()
    return s


def eo(title="The Better Deal Affinity Range", **kw) -> ExtractedOffer:
    d = dict(
        title=title,
        benefits=[Benefit(mechanism_type="upgrade_inclusion_package", label="Luxury inclusions", amount=82000, amount_qualifier="up_to", amount_kind="cash_saving",
                          evidence="Receive up to $82K in luxury inclusions on selected Affinity designs."),
                  Benefit(mechanism_type="deposit_assistance", label="$2,000 until slab", amount=None, customer_payment_required=2000, amount_kind="payment_arrangement",
                          evidence="Only $2,000 to pay until slab when financed through Partner Lender.")],
        eligibility=Eligibility(ranges=["Affinity"], finance_requirements=["financed through Partner Lender"], exclusions=["knockdown rebuild"],
                                evidence=["Only $2,000 to pay until slab when financed through Partner Lender.", "Offer excludes knockdown rebuild."]),
        dates=OfferDates(expiry_date="2026-11-30", expiry_text="Offer ends 30 November 2026", evidence="Offer ends 30 November 2026."))
    d.update(kw)
    return ExtractedOffer(**d)


def cycle(db, monkeypatch, source, html=None, llm=None, fail=None, now=None):
    """One collection cycle: fetch (fake network) -> snapshot -> extraction/failure handling."""
    res = fail_result(source.url, *fail) if fail else ok_result(source.url, html)
    monkeypatch.setattr(fetcher, "fetch_url", lambda url, **k: res)
    snap, _ = fetcher.fetch_source(db, source)
    if snap.outcome == "ok":
        out = pipeline.process_source(db, source, snap, llm, now)
    else:
        pipeline.record_failure(db, source, snap, now)
        out = {}
    db.commit()
    return snap, out


def only_offer(db) -> Offer:
    return db.scalars(select(Offer)).one()


# --------------------------------------------------------------------------- capture, conditions, value types
def test_conditions_customer_payment_and_up_to_survive_extraction_and_export(seeded, monkeypatch):
    s = src(seeded)
    llm = FakeLLM(PageExtraction(offers=[eo()]))
    cycle(seeded, monkeypatch, s, page(MAIN), llm)
    o = only_offer(seeded)
    inc = next(m for m in o.mechanisms if m.mechanism_type == "upgrade_inclusion_package")
    dep = next(m for m in o.mechanisms if m.mechanism_type == "deposit_assistance")
    assert (inc.amount, inc.amount_qualifier) == (82000, "up_to")
    assert inc.amount_kind == "inclusion_value"  # a claimed inclusion value is never a cash saving, even if the model said so
    assert dep.customer_payment_required == 2000 and dep.amount is None and dep.amount_kind == "payment_arrangement"
    d = Q.offer_dict(seeded, o)
    assert d["finance_requirements"] == ["financed through Partner Lender"] and d["exclusions"] == ["knockdown rebuild"]
    assert "Up to $82,000 claimed inclusion value" in d["value_summary"] and "customer pays $2,000" in d["value_summary"]
    assert o.published_expiry == date(2026, 11, 30) and d["expiry_label"] == "2026-11-30"


def test_government_grant_is_not_builder_funded_and_unpriced_stays_unknown(seeded, monkeypatch):
    s = src(seeded)
    html = page("<h2>First Home Boost</h2><p>Eligible buyers may receive the $10,000 First Home Owner Grant from government.</p><p>Free site costs on selected packages.</p>")
    ex = ExtractedOffer(title="First Home Boost", benefits=[
        Benefit(mechanism_type="government_grant", label="FHOG", amount=10000, amount_kind="cash_saving", evidence="may receive the $10,000 First Home Owner Grant from government."),
        Benefit(mechanism_type="site_costs", label="Free site costs", amount=5000, amount_kind="cash_saving", evidence="Free site costs on selected packages.")])
    cycle(seeded, monkeypatch, s, html, FakeLLM(PageExtraction(offers=[ex])))
    o = only_offer(seeded)
    grant = next(m for m in o.mechanisms if m.mechanism_type == "government_grant")
    assert grant.government_component and grant.amount_kind == "government_grant"
    site = next(m for m in o.mechanisms if m.mechanism_type == "site_costs")
    assert site.amount is None  # the model invented $5,000; it is not in the quote so it is shown as unknown
    assert any("not in its quote" in n for n in o.versions[-1].unresolved)


def test_unsupported_quotes_and_dates_are_dropped(seeded, monkeypatch):
    s = src(seeded)
    bad = ExtractedOffer(title="Mystery", benefits=[Benefit(mechanism_type="cash_discount", label="Big saving", amount=99999, evidence="Save $99,999 on everything today")],
                         dates=OfferDates(expiry_date="2026-12-25", evidence="ends 25 December 2026"))
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[bad, eo()])))
    o = only_offer(seeded)  # the unverifiable offer never becomes a record
    assert o.title == "The Better Deal Affinity Range"


def test_verification_helpers():
    assert verify.money_values("Receive up to $82K and $1.5m or $2,000.50") == [82000, 1_500_000, 2000.5]
    assert verify.quote_in_text("Up to $82K in luxury inclusions", verify.norm("receive UP TO $82K in luxury   inclusions on designs"))
    assert not verify.quote_in_text("Save $99,999 today", verify.norm("receive up to $82K"))
    assert verify.date_supported("2026-11-30", "ends 30th November 2026", "Offer ends 30th November 2026")[0] == date(2026, 11, 30)
    assert verify.date_supported("2026-11-30", "ends 30th November 2026", "nothing relevant here")[0] is None  # a quote that is not on the page proves nothing
    assert verify.date_supported("2026-11-30", None, "valid until 30/11/26")[0] == date(2026, 11, 30)
    assert verify.date_supported("2026-12-01", "ends 30 November 2026", "ends 30 November 2026")[0] is None
    d, inferred = verify.date_supported("2026-10-31", "ends 31 October", "ends 31 October")
    assert d == date(2026, 10, 31) and inferred  # no year printed -> flagged as inferred


# --------------------------------------------------------------------------- geography
def test_southern_nsw_offer_is_not_a_sydney_offer(seeded, monkeypatch):
    s = src(seeded, url="https://www.clarendon.com.au/special", slug="clarendon-homes", state=None)
    html = page("<h2>Southern NSW Facade Offer</h2><p>Free facade upgrade for new contracts in Southern NSW only. Ends 31 December 2026.</p>")
    ex = ExtractedOffer(title="Southern NSW Facade Offer",
                        benefits=[Benefit(mechanism_type="facade_benefit", label="Free facade upgrade", evidence="Free facade upgrade for new contracts in Southern NSW only.")],
                        geography=[GeoScope(level="build_region", state="NSW", name="Southern NSW", as_stated="Southern NSW", evidence="new contracts in Southern NSW only")],
                        dates=OfferDates(expiry_date="2026-12-31", evidence="Ends 31 December 2026."))
    cycle(seeded, monkeypatch, s, html, FakeLLM(PageExtraction(offers=[ex])))
    o = only_offer(seeded)
    assert [(a.state, a.name, a.scope_level) for a in o.applicability] == [("NSW", "Southern NSW", "build_region")]
    d = Q.offer_dict(seeded, o)
    assert "Sydney" not in d["geography_label"] and "Southern NSW" in d["geography_label"]
    assert o in Q.list_offers(seeded, state="NSW") and o not in Q.list_offers(seeded, state="VIC") and o not in Q.list_offers(seeded, state="QLD")


def test_national_page_without_stated_region_is_not_assumed_everywhere(seeded, monkeypatch):
    s = src(seeded, url="https://www.clarendon.com.au/offers-national", slug="clarendon-homes", state=None)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    o = only_offer(seeded)
    assert not o.applicability and Q.offer_dict(seeded, o)["geography_label"] == "Geography not stated"
    # Clarendon trades in NSW and QLD only: shown there flagged as unknown geography, never in VIC or SA
    assert o in Q.list_offers(seeded, state="NSW") and o not in Q.list_offers(seeded, state="VIC") and o not in Q.list_offers(seeded, state="SA")
    assert o not in Q.list_offers(seeded, state="NSW", include_unknown_geo=False)


def test_duplicate_regional_pages_make_one_campaign_with_both_regions(seeded, monkeypatch):
    mel = src(seeded, url="https://www.metricon.example/latest-offers/melbourne", slug="carlisle-homes", ctx={"region": "melbourne", "scope_level": "metro", "area": "Melbourne"})
    gee = src(seeded, url="https://www.metricon.example/latest-offers/vic-regional", slug="carlisle-homes", ctx={"region": "vic-regional", "scope_level": "build_region", "area": "Regional Victoria"})
    llm = FakeLLM(PageExtraction(offers=[eo()]))
    cycle(seeded, monkeypatch, mel, page(MAIN), llm)
    cycle(seeded, monkeypatch, gee, page(MAIN), llm)
    o = only_offer(seeded)  # same title, same mechanisms/amounts/expiry => one campaign
    assert seeded.scalar(select(func.count()).select_from(OfferSource).where(OfferSource.offer_id == o.id)) == 2
    assert sorted(a.name for a in o.applicability) == ["Melbourne", "Regional Victoria"]
    assert all("[page_context]" in a.as_stated for a in o.applicability)  # recorded as page context, not stated text


def test_same_title_but_different_conditions_is_kept_separate_and_flagged(seeded, monkeypatch):
    a = src(seeded, url="https://x.example/a", slug="carlisle-homes", ctx={"region": "a"})
    b = src(seeded, url="https://x.example/b", slug="carlisle-homes", ctx={"region": "b"})
    other = eo()
    other.benefits[0].amount = 48000
    other.benefits[0].evidence = "Receive up to $48K in luxury inclusions on selected Affinity designs."
    cycle(seeded, monkeypatch, a, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    cycle(seeded, monkeypatch, b, page(MAIN.replace("$82K", "$48K")), FakeLLM(PageExtraction(offers=[other])))
    assert seeded.scalar(select(func.count()).select_from(Offer)) == 2
    assert any("different conditions" in " ".join(o.review_reasons) for o in seeded.scalars(select(Offer)))


# --------------------------------------------------------------------------- freshness, status
def test_failed_fetch_keeps_offer_and_marks_stale_never_withdrawn(seeded, monkeypatch):
    s = src(seeded)
    llm = FakeLLM(PageExtraction(offers=[eo()]))
    cycle(seeded, monkeypatch, s, page(MAIN), llm)
    o = only_offer(seeded)
    assert o.status == "advertised" and Q.offer_dict(seeded, o)["freshness"] == "fresh"
    for _ in range(3):
        cycle(seeded, monkeypatch, s, fail=("timeout", "timed out"))
    seeded.refresh(o)
    assert o.status == "advertised"  # a failed fetch cannot withdraw anything
    d = Q.offer_dict(seeded, o)
    assert d["freshness"] == "stale" and "timeout" in d["failure_reason"] and o.last_failed_attempt_at is not None
    assert s.consecutive_failures == 3 and s.last_outcome == "timeout"
    cycle(seeded, monkeypatch, s, page(MAIN), llm)
    seeded.refresh(o)
    assert Q.offer_dict(seeded, o)["freshness"] == "fresh" and o.status == "advertised"


def test_blocked_page_is_recorded_as_blocked_and_changes_nothing(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    cycle(seeded, monkeypatch, s, fail=("blocked", "bot-protection challenge returned instead of content (not bypassed)"))
    assert s.last_outcome == "blocked" and only_offer(seeded).status == "advertised"


def test_successful_crawl_without_the_campaign_means_no_longer_observed_not_withdrawn(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    cycle(seeded, monkeypatch, s, page("<p>Welcome to our latest news and stories about building.</p>"), FakeLLM(PageExtraction(offers=[])))
    o = only_offer(seeded)
    assert o.status == "no_longer_observed" and o.status != "withdrawn"
    assert "not confirmed withdrawn" in o.status_reason
    cycle(seeded, monkeypatch, s, page(MAIN + "<p>Welcome back</p>"), FakeLLM(PageExtraction(offers=[eo()])))
    seeded.refresh(o)
    assert o.status == "advertised"  # re-observed


def test_withdrawn_needs_a_quote_that_exists(seeded, monkeypatch):
    s = src(seeded)
    w = eo(withdrawn_explicit=True, withdrawn_evidence="This offer has ended and is no longer available")
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[w])))
    assert only_offer(seeded).status == "advertised"  # quote is not on the page, so the flag is discarded
    cycle(seeded, monkeypatch, s, page(MAIN + "<p>This offer has ended and is no longer available</p>"), FakeLLM(PageExtraction(offers=[w])))
    assert only_offer(seeded).status == "withdrawn"


def test_published_expiry_expires_offer_and_limited_time_does_not(seeded, monkeypatch):
    s = src(seeded)
    past = eo(dates=OfferDates(expiry_date="2026-10-01", evidence="Offer ends 1 October 2026."), title="Old")
    limited = eo(title="Limited", dates=OfferDates(expiry_text="for a limited time", evidence="for a limited time"))
    html = page(MAIN + "<p>Offer ends 1 October 2026.</p><p>for a limited time</p>")
    cycle(seeded, monkeypatch, s, html, FakeLLM(PageExtraction(offers=[past, limited])))
    by = {o.title: o for o in seeded.scalars(select(Offer))}
    assert by["Old"].status == "expired" and by["Old"].expiry_timezone_assumed if hasattr(by["Old"], "expiry_timezone_assumed") else by["Old"].status == "expired"
    assert by["Limited"].status == "advertised" and by["Limited"].published_expiry is None
    assert Q.offer_dict(seeded, by["Limited"])["expiry_label"] == "Expiry not published"


def test_pdf_or_terms_only_evidence_is_not_a_current_advert(seeded):
    from app.models import Brand

    b = seeded.scalar(select(Brand).where(Brand.slug == "carlisle-homes"))
    pdf = src(seeded, url="https://files.example/old-offer.pdf", kind="pdf")
    o = Offer(campaign_id="OFR-TEST0001", brand_id=b.id, campaign_key="old", title="Old PDF offer")
    seeded.add(o)
    seeded.flush()
    o.sources.append(OfferSource(offer_id=o.id, source_id=pdf.id, role="pdf"))
    pipeline.rebuild_derived(seeded, o)
    assert o.status == "needs_review" and "terms/PDF" in o.status_reason


# --------------------------------------------------------------------------- noise, history, baseline
def test_template_only_changes_create_no_new_version_extraction_or_event(seeded, monkeypatch):
    s = src(seeded)
    llm = FakeLLM(PageExtraction(offers=[eo()]))
    cycle(seeded, monkeypatch, s, page(MAIN, banner="Call us now 1300 000 000", footer_extra="12:01:33 session=abcdefghijklmnopqrstuvwxyz0123456789"), llm)
    snap2, out = cycle(seeded, monkeypatch, s, page(MAIN, banner="Free consult this week!", footer_extra="14:45:10 session=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"), llm)
    assert llm.calls == 1 and out.get("skipped_unchanged")
    assert snap2.unchanged_of_id is not None and snap2.clean_text is None  # body not stored twice
    o = only_offer(seeded)
    assert len(o.versions) == 1
    assert events.events_from_offers(seeded, datetime(2020, 1, 1, tzinfo=timezone.utc)) == 0
    assert seeded.scalar(select(func.count()).select_from(CompetitorEvent)) == 0


def test_first_capture_is_baseline_later_offer_is_not(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    first = only_offer(seeded)
    assert first.is_baseline and first.versions[0].is_baseline
    new = eo(title="Brand New Campaign", benefits=[Benefit(mechanism_type="price_hold", label="12 month price lock", evidence="12 month price lock on all new contracts.")])
    cycle(seeded, monkeypatch, s, page(MAIN + "<p>12 month price lock on all new contracts.</p>"), FakeLLM(PageExtraction(offers=[eo(), new])))
    by = {o.title: o for o in seeded.scalars(select(Offer))}
    assert by["Brand New Campaign"].is_baseline is False
    n = events.events_from_offers(seeded, datetime(2020, 1, 1, tzinfo=timezone.utc))
    kinds = [e.event_type for e in seeded.scalars(select(CompetitorEvent))]
    assert kinds == ["new_offer"] and n == 1  # the baseline offer is not reported as news


def test_material_change_makes_new_version_with_field_diff_and_event(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    stronger = eo()
    stronger.benefits[0].amount = 90000
    stronger.benefits[0].evidence = "Receive up to $90K in luxury inclusions on selected Affinity designs."
    stronger.dates = OfferDates(expiry_date="2026-12-31", evidence="Offer ends 31 December 2026.")
    html2 = page(MAIN.replace("$82K", "$90K").replace("30 November", "31 December"))
    cycle(seeded, monkeypatch, s, html2, FakeLLM(PageExtraction(offers=[stronger])))
    o = only_offer(seeded)
    assert [v.version_no for v in o.versions] == [1, 2] and o.published_expiry == date(2026, 12, 31)
    fields = {d["field"] for d in o.versions[1].diff}
    assert "dates.expiry_date" in fields and any(f.startswith("benefit[upgrade_inclusion_package") for f in fields)
    assert o.first_observed_at <= o.versions[1].observed_at  # first observed stays distinct from the later change
    assert events.events_from_offers(seeded, datetime(2020, 1, 1, tzinfo=timezone.utc)) == 1
    p = Q.promo_patterns(seeded, "VIC")
    inc = next(m for m in p["mechanisms"] if m["type"] == "upgrade_inclusion_package")
    assert inc["strengthened"] == 1 and inc["extended"] == 1
    assert "not every Australian builder" in p["note"] and p["roster_size"] == 7


def test_rules_mode_without_model_flags_everything_for_review(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page("<h2>Up to $82K in luxury inclusions</h2><p>Offer ends 30 November 2026.</p>"), llm=None)
    o = only_offer(seeded)
    assert o.versions[-1].extraction_method == "rules" and o.needs_review
    assert any("rules only" in r for r in o.review_reasons)


# --------------------------------------------------------------------------- corrections, conflicts
def test_human_corrections_persist_across_refresh_with_audit_trail(seeded, monkeypatch):
    s = src(seeded)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    o = only_offer(seeded)
    pipeline.apply_correction(seeded, o, "published_expiry", "2026-12-15", None, "Terms PDF says 15 Dec")
    pipeline.apply_correction(seeded, o, "geography", [{"level": "state", "state": "VIC", "name": "Victoria", "as_stated": "Victoria", "basis": "stated"}], None, "Confirmed by sales")
    seeded.commit()
    assert o.published_expiry == date(2026, 12, 15)
    stronger = eo()
    stronger.benefits[0].amount, stronger.benefits[0].evidence = 90000, "Receive up to $90K in luxury inclusions on selected Affinity designs."
    cycle(seeded, monkeypatch, s, page(MAIN.replace("$82K", "$90K")), FakeLLM(PageExtraction(offers=[stronger])))
    seeded.refresh(o)
    assert len(o.versions) == 2 and o.mechanisms[0].amount == 90000  # the refresh updated the machine facts ...
    assert o.published_expiry == date(2026, 12, 15)  # ... without erasing the correction
    assert [(a.state, a.name) for a in o.applicability] == [("VIC", "Victoria")]
    assert o.versions[1].fields["dates"]["expiry_date"] == "2026-11-30"  # machine extraction kept alongside
    log = seeded.scalars(select(AuditLog).where(AuditLog.action == "offer.correct")).all()
    assert len(log) == 2 and log[0].before and log[0].after


def test_human_status_decision_survives_refresh_until_released(seeded, monkeypatch):
    s = src(seeded)
    llm = FakeLLM(PageExtraction(offers=[eo()]))
    cycle(seeded, monkeypatch, s, page(MAIN), llm)
    o = only_offer(seeded)
    pipeline.apply_correction(seeded, o, "status", "withdrawn", None, "Sales confirmed withdrawn")
    seeded.commit()
    cycle(seeded, monkeypatch, s, page(MAIN + "<p>extra</p>"), llm)
    seeded.refresh(o)
    assert o.status == "withdrawn" and o.status_set_by_human
    pipeline.release_status(seeded, o, None)
    assert o.status == "advertised"


def test_landing_vs_terms_conflict_is_retained_and_requires_review(seeded, monkeypatch):
    from app.services.offers import llm_extract

    s = src(seeded)
    cycle(seeded, monkeypatch, s, page(MAIN), FakeLLM(PageExtraction(offers=[eo()])))
    o = only_offer(seeded)
    o.terms_url = "https://www.carlislehomes.com.au/disclaimer/"
    terms = src(seeded, url=o.terms_url, kind="terms")
    terms_text = "Affinity Range offer: complimentary upgrades. This offer closes 15 December 2026 and is limited to contracts signed in Victoria."
    monkeypatch.setattr(fetcher, "fetch_url", lambda url, **k: ok_result(url, page(f"<p>{terms_text}</p>")))
    tsnap, _ = fetcher.fetch_source(seeded, terms)
    te = llm_extract.TermsEnrichment(eligibility=Eligibility(contract_condition="contracts signed in Victoria", evidence=["limited to contracts signed in Victoria"]),
                                     dates=OfferDates(expiry_date="2026-12-15", evidence="This offer closes 15 December 2026"))
    assert pipeline.enrich_with_terms(seeded, o, terms, tsnap, FakeLLM(te))
    seeded.commit()
    seeded.refresh(o)
    assert o.conflicts and "differs from terms expiry" in o.conflicts[0]
    assert o.needs_review and any("conflict" in r.lower() for r in o.review_reasons)
    assert o.versions[-1].fields["dates"]["expiry_date"] == "2026-11-30"  # landing value retained, terms value stored beside it
    assert o.versions[-1].fields["terms_enrichment"]["dates"]["expiry_date"] == "2026-12-15"
    assert Q.offer_dict(seeded, o)["contract_condition"] == "contracts signed in Victoria"
