"""A failed extraction must never make offers disappear (regression: AI errors turned every offer into 'no longer observed')."""
from sqlalchemy import select

from app.models import Offer
from app.services.llm import LLMError
from app.services.offers.schema import PageExtraction
from tests.helpers import FakeLLM, page
from tests.test_offers import MAIN, cycle, eo, src


def boom(_user):
    raise LLMError("the model rejected the request")


def test_model_failure_leaves_offers_advertised_and_fresh_data_unchanged(seeded, monkeypatch):
    s = src(seeded)
    html = page(MAIN)
    cycle(seeded, monkeypatch, s, html, FakeLLM(PageExtraction(offers=[eo()])))
    o = seeded.scalars(select(Offer)).one()
    assert o.status == "advertised"
    for _ in range(3):  # three days of failing extraction while the page itself keeps loading fine
        cycle(seeded, monkeypatch, s, html + "<p>changed so the promo window differs</p><p>Save $1,000 extra</p>", FakeLLM(boom))
    seeded.refresh(o)
    assert o.status == "advertised"  # was: no_longer_observed
    assert len(o.versions) == 1
    # and once the model works again the offer is simply re-read
    cycle(seeded, monkeypatch, s, html, FakeLLM(PageExtraction(offers=[eo()])))
    seeded.refresh(o)
    assert o.status == "advertised"
