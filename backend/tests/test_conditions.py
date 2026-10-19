"""Market-conditions reading: transparent rules over stored official statistics, state comparison, RBA table parsing, release calendar. SYNTHETIC observations."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models import MacroRelease, MacroSeries
from app.services import macro


def put(db, code, values: list[tuple[str, float]], pub=None, nxt=None):
    macro.ensure_series(db)
    s = db.scalar(select(MacroSeries).where(MacroSeries.code == code))
    macro.upsert_observations(db, s, [(p, Decimal(str(v)), None) for p, v in values], None)
    if pub or nxt:
        db.add(MacroRelease(source=s.source, dataset=s.dataset, release_key="k", reference_period=values[-1][0], publication_date=pub, detail={"next_release": nxt} if nxt else {}))
    db.flush()
    return s


def months(start_year, start_month, vals):
    out, y, m = [], start_year, start_month
    for v in vals:
        out.append((f"{y}-{m:02d}", v))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def test_approvals_rule_uses_three_months_vs_the_same_three_a_year_earlier(seeded):
    put(seeded, "abs_ba_private_houses_vic", months(2025, 6, [1000] * 12 + [1200, 1200, 1200]))  # Jun25..Aug26 : last 3 are 3600 vs 3000
    c = macro.conditions(seeded, "VIC")
    a = next(i for i in c["items"] if i["indicator"] == "house_approvals")
    assert a["stance"] == "supportive" and "+20.0%" in a["basis"] and a["scope"] == "VIC" and "Last 3 months" in a["rule"] and a["use"]


def test_falling_approvals_are_weaker_and_flat_is_flat(seeded):
    put(seeded, "abs_ba_private_houses_nsw", months(2025, 6, [1000] * 12 + [800, 800, 800]))
    put(seeded, "abs_ba_private_houses_qld", months(2025, 6, [1000] * 12 + [1010, 1010, 1010]))
    assert next(i for i in macro.conditions(seeded, "NSW")["items"] if i["indicator"] == "house_approvals")["stance"] == "weaker"
    assert next(i for i in macro.conditions(seeded, "QLD")["items"] if i["indicator"] == "house_approvals")["stance"] == "flat"


def test_not_enough_history_is_unknown_not_a_guess(seeded):
    put(seeded, "abs_ba_private_houses_sa", months(2026, 1, [1000, 1000, 1000]))
    a = next(i for i in macro.conditions(seeded, "SA")["items"] if i["indicator"] == "house_approvals")
    assert a["stance"] == "unknown" and a["use"] is None


def test_state_with_no_series_falls_back_to_the_national_one_and_says_so(seeded):
    put(seeded, "abs_ba_private_houses_au", months(2025, 6, [1000] * 12 + [1200, 1200, 1200]))
    seeded.execute(MacroSeries.__table__.update().where(MacroSeries.code == "abs_ba_private_houses_vic").values(active=False))
    a = next(i for i in macro.conditions(seeded, "VIC")["items"] if i["indicator"] == "house_approvals")
    assert a["geography"] == "AU" and a["scope"] == "national series"


def test_rates_compare_with_twelve_months_earlier(seeded):
    put(seeded, "rba_mortgage_rate_variable_discounted", months(2025, 9, [6.05] * 12 + [6.8]))
    put(seeded, "rba_cash_rate_target", [("2025-09-30", 3.6), ("2026-09-30", 4.6)])
    items = {i["indicator"]: i for i in macro.conditions(seeded, "AU")["items"]}
    assert items["mortgage_rate"]["stance"] == "headwind" and "+0.75 pts" in items["mortgage_rate"]["basis"]
    assert items["cash_rate"]["stance"] == "headwind" and "+1.00 pts" in items["cash_rate"]["basis"]


def test_summary_counts_and_never_forecasts(seeded):
    put(seeded, "abs_ba_private_houses_au", months(2025, 6, [1000] * 12 + [1200, 1200, 1200]))
    put(seeded, "rba_cash_rate_target", [("2025-09-30", 3.6), ("2026-09-30", 4.6)])
    c = macro.conditions(seeded, "AU")
    assert "supportive" in c["summary"] and "forecast" in c["note"]


def test_state_comparison_lines_states_up_side_by_side(seeded):
    for g, v in (("vic", 1200), ("nsw", 900), ("au", 1050)):
        put(seeded, f"abs_ba_private_houses_{g}", months(2025, 6, [1000] * 12 + [v] * 3))
    row = next(r for r in macro.compare_states(seeded)["rows"] if r["indicator"] == "house_approvals")
    assert row["cells"]["VIC"]["stance"] == "supportive" and row["cells"]["NSW"]["stance"] == "weaker" and row["cells"]["QLD"] is None


F5 = """﻿F5  INDICATOR LENDING RATES
Title,Lending rates; Housing loans; Banks; Variable; Standard; Owner-occupier,Lending rates; Housing loans; Banks; Variable; Discounted; Owner-occupier
Description,a,b
Frequency,Monthly,Monthly
Series ID,FILRHLBVS,FILRHLBVD
Publication date,08-Oct-2026,08-Oct-2026
31-Jul-2026,8.40,6.70
31-Aug-2026,8.50,6.80
"""


def test_rba_table_parser_finds_the_column_by_title():
    rows, sid, pub = macro.parse_rba_table(F5, "Lending rates; Housing loans; Banks; Variable; Discounted; Owner-occupier")
    assert rows == [("2026-07", Decimal("6.70"), None), ("2026-08", Decimal("6.80"), None)] and sid == "FILRHLBVD" and pub == date(2026, 10, 8)
    import pytest

    with pytest.raises(ValueError):
        macro.parse_rba_table(F5, "Not a column")


def test_release_calendar_sorts_by_next_release_and_leaves_unknowns_blank(seeded):
    put(seeded, "abs_ba_private_houses_au", months(2026, 1, [1000, 1000]), pub=date(2026, 10, 1), nxt="05/11/2026")
    put(seeded, "abs_commenced_private_houses_au", [("2026-Q1", 100.0)], pub=date(2026, 7, 7), nxt="13/01/2027")
    put(seeded, "rba_cash_rate_target", [("2026-09-30", 4.6)])
    cal = macro.release_calendar(seeded)
    assert [c["name"] for c in cal[:2]] == ["ABS Building Approvals", "ABS Building Activity"] and cal[0]["next_release"] == "2026-11-05"
    assert cal[-1]["next_release"] is None


def test_default_series_include_commencements_and_mortgage_rates():
    codes = {d["code"] for d in macro.default_series()}
    assert {"abs_commenced_private_houses_vic", "abs_commenced_private_houses_au", "rba_mortgage_rate_variable_discounted", "rba_mortgage_rate_fixed_3yr"} <= codes
