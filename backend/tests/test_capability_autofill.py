from sqlalchemy import select

from app.models import TrendCapability
from app.services.trends import service as T
from tests.test_trends import prov  # noqa: F401  (fixture)


def test_normal_refresh_fills_the_coverage_table_and_flags_missing_locations(seeded, prov):
    p, client = prov
    T.fetch_compare(seeded, client, ["metricon", "arden homes"], "VIC")
    try:
        T.fetch_compare(seeded, client, ["metricon", "arden homes"], "SA")  # absent from the mocked catalogue
    except Exception:
        pass
    seeded.commit()
    caps = {c.geo_key: c for c in seeded.scalars(select(TrendCapability))}
    assert caps["VIC"].supported and caps["VIC"].coverage in ("full", "sparse") and caps["VIC"].tested_at
    assert caps["SA"].supported is False and "catalogue" in caps["SA"].detail["reason"]
