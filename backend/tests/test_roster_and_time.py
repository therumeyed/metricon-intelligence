from datetime import datetime, timezone

from sqlalchemy import func, select

from app.models import Brand, BrandRegion
from app.services import jobs
from app.timeutil import is_expired


def test_roster_is_16_brands_22_assignments(seeded):
    assert seeded.scalar(select(func.count()).select_from(Brand)) == 16
    assert seeded.scalar(select(func.count()).select_from(BrandRegion)) == 22
    by_state = dict(seeded.execute(select(BrandRegion.state, func.count()).group_by(BrandRegion.state)).all())
    assert by_state == {"VIC": 7, "NSW": 5, "QLD": 5, "SA": 5}
    gjg = seeded.scalar(select(Brand).where(Brand.slug == "gj-gardner"))
    assert sorted(r.state for r in gjg.regions) == ["NSW", "QLD", "VIC"]  # one canonical brand, regions stored separately
    assert {a.alias for a in gjg.aliases} >= {"gj gardner homes", "g.j. gardner homes"}


def test_seed_is_idempotent(seeded):
    from app.seed import seed_all

    out = seed_all(seeded)
    assert out["brands"] == 0 and out["regions"] == 0 and out["sources"] == 0


def test_expiry_respects_stated_timezone():
    from datetime import date

    d = date(2026, 11, 30)
    # 30 Nov 23:59 Brisbane (UTC+10) == 13:59 UTC; Adelaide (UTC+10:30 in Nov) ends earlier in UTC than Perth
    assert not is_expired(d, "Australia/Brisbane", datetime(2026, 11, 30, 13, 0, tzinfo=timezone.utc))
    assert is_expired(d, "Australia/Brisbane", datetime(2026, 11, 30, 14, 5, tzinfo=timezone.utc))
    assert is_expired(d, "Australia/Perth", datetime(2026, 11, 30, 16, 5, tzinfo=timezone.utc)) is True
    assert not is_expired(None, None)  # 'limited time' never expires by itself


def test_schedule_uses_sydney_time_across_daylight_saving():
    sch = next(s for s in jobs.SCHEDULES if s.job == "collect_sources")  # 05:00 Sydney
    # Summer (AEDT, UTC+11): 05:00 Sydney == 18:00 UTC previous day
    slot = jobs.latest_slot(sch, datetime(2026, 12, 10, 3, 0, tzinfo=timezone.utc))  # 14:00 Sydney on 10 Dec
    assert slot == datetime(2026, 12, 9, 18, 0, tzinfo=timezone.utc)
    # Winter (AEST, UTC+10): 05:00 Sydney == 19:00 UTC previous day
    slot = jobs.latest_slot(sch, datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc))
    assert slot == datetime(2026, 7, 9, 19, 0, tzinfo=timezone.utc)
    weekly = next(s for s in jobs.SCHEDULES if s.job == "refresh_trends")
    assert jobs.latest_slot(weekly, datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)).astimezone(jobs.app_tz()).weekday() == 0
