"""Period-change maths for Google Trends series. Pure functions; no I/O.

Rules (brief s8):
* Compare the average of the latest 4 complete weeks with the preceding 4 complete weeks, from the *same* fetched series.
* Year-on-year: the same 4-week window one year earlier, from the same series, when history permits.
* Incomplete observations (provider `missing_data` flag, or a period that has not finished yet) are excluded.
* Need enough observations and a meaningful non-zero baseline; otherwise return 'unavailable' with a reason, never a
  misleading percentage. A provider 0 means 'insufficient data' and is never treated as a real zero demand.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MIN_BASELINE = 5.0  # index points; below this a percentage change is noise
WINDOW = 4


@dataclass(frozen=True)
class Point:
    start: date
    end: date | None
    value: int | None
    partial: bool = False


@dataclass
class Change:
    status: str  # ok | unavailable
    value_pct: float | None = None
    current_avg: float | None = None
    comparison_avg: float | None = None
    reason: str | None = None
    current_window: tuple[date, date] | None = None
    comparison_window: tuple[date, date] | None = None
    points_used: int = 0

    def as_dict(self) -> dict:
        return {"status": self.status, "value_pct": self.value_pct, "current_avg": self.current_avg, "comparison_avg": self.comparison_avg,
                "reason": self.reason, "points_used": self.points_used,
                "current_window": [d.isoformat() for d in self.current_window] if self.current_window else None,
                "comparison_window": [d.isoformat() for d in self.comparison_window] if self.comparison_window else None}


READABLE_REASONS = {
    "insufficient_observations": "Insufficient search data",
    "baseline_too_low": "Insufficient search data (baseline too low for a percentage)",
    "no_year_ago_history": "Not enough history for a year-on-year comparison",
    "gaps_in_series": "Missing weeks in the series",
    "provider_zero": "Insufficient search data",
}


def complete_points(points: list[Point], today: date) -> list[Point]:
    """Drop partial/in-progress/missing observations and order by period."""
    out = []
    for p in points:
        if p.value is None or p.partial:
            continue
        if p.end is not None and p.end >= today:  # period still running
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.start)


def _is_consecutive_weeks(window: list[Point]) -> bool:
    return all(0 < (b.start - a.start).days <= 8 for a, b in zip(window, window[1:]))


def _avg(window: list[Point]) -> float:
    return sum(p.value for p in window) / len(window)  # type: ignore[misc]


def _compare(cur: list[Point], base: list[Point], missing_reason: str = "insufficient_observations") -> Change:
    if len(cur) < WINDOW or len(base) < WINDOW:
        return Change("unavailable", reason=missing_reason)
    if not (_is_consecutive_weeks(cur) and _is_consecutive_weeks(base)):
        return Change("unavailable", reason="gaps_in_series")
    ca, ba = _avg(cur), _avg(base)
    win = (cur[0].start, cur[-1].end or cur[-1].start)
    bwin = (base[0].start, base[-1].end or base[-1].start)
    if ba < MIN_BASELINE:
        return Change("unavailable", current_avg=ca, comparison_avg=ba, reason="baseline_too_low", current_window=win, comparison_window=bwin, points_used=len(cur) + len(base))
    return Change("ok", value_pct=round((ca - ba) / ba * 100, 1), current_avg=round(ca, 2), comparison_avg=round(ba, 2),
                  current_window=win, comparison_window=bwin, points_used=len(cur) + len(base))


def four_week_change(points: list[Point], today: date) -> Change:
    pts = complete_points(points, today)
    if len(pts) < WINDOW * 2:
        return Change("unavailable", reason="insufficient_observations")
    cur, base = pts[-WINDOW:], pts[-2 * WINDOW:-WINDOW]
    return _compare(cur, base)


def year_on_year(points: list[Point], today: date) -> Change:
    pts = complete_points(points, today)
    if len(pts) < WINDOW:
        return Change("unavailable", reason="insufficient_observations")
    cur = pts[-WINDOW:]
    target = cur[0].start - timedelta(days=364)
    # align by period start within +/- 3 days
    base: list[Point] = []
    for i in range(WINDOW):
        want = target + timedelta(days=7 * i)
        match = next((p for p in pts if abs((p.start - want).days) <= 3), None)
        if match is None:
            return Change("unavailable", reason="no_year_ago_history")
        base.append(match)
    return _compare(cur, base, "no_year_ago_history")


def normalise_for_sparkline(points: list[Point]) -> list[dict]:
    """Individually normalised 0-100 series for roster sparklines. Labelled as such in the UI; never comparable across brands."""
    vals = [p.value for p in points if p.value is not None]
    mx = max(vals) if vals else 0
    out = []
    for p in sorted(points, key=lambda p: p.start):
        if p.value is None:
            out.append({"date": p.start.isoformat(), "v": None})
        else:
            out.append({"date": p.start.isoformat(), "v": round(p.value / mx * 100, 1) if mx else None})
    return out
