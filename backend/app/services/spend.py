"""Provider spend + call limits. Reported and estimated costs are stored separately (brief s13)."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppSetting, SpendLedger
from ..timeutil import app_tz, now_utc


class BudgetExceeded(RuntimeError):
    pass


def _setting(db: Session, key: str, default):
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def limits(db: Session) -> dict:
    s = get_settings()
    return {
        "dataforseo_daily_usd": float(_setting(db, "dataforseo_daily_usd_limit", s.dataforseo_daily_usd_limit)),
        "dataforseo_monthly_usd": float(_setting(db, "dataforseo_monthly_usd_limit", s.dataforseo_monthly_usd_limit)),
        "dataforseo_max_requests_per_job": int(_setting(db, "dataforseo_max_requests_per_job", s.dataforseo_max_requests_per_job)),
        "anthropic_max_calls_per_job": int(_setting(db, "anthropic_max_calls_per_job", s.anthropic_max_calls_per_job)),
        "anthropic_daily_calls": int(_setting(db, "anthropic_daily_call_limit", s.anthropic_daily_call_limit)),
    }


def _bounds():
    now = now_utc().astimezone(app_tz())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return day.astimezone(timezone.utc), month.astimezone(timezone.utc)


def spent(db: Session, provider: str) -> dict:
    day, month = _bounds()
    out = {}
    for label, since in (("day", day), ("month", month)):
        for kind in ("reported", "estimated"):
            v = db.scalar(select(func.coalesce(func.sum(SpendLedger.amount_usd), 0)).where(
                SpendLedger.provider == provider, SpendLedger.kind == kind, SpendLedger.at >= since))
            out[f"{label}_{kind}"] = float(v or 0)
        out[f"{label}_calls"] = int(db.scalar(select(func.coalesce(func.sum(SpendLedger.calls), 0)).where(
            SpendLedger.provider == provider, SpendLedger.at >= since, SpendLedger.kind == "estimated")) or 0)
    return out


def check_dataforseo(db: Session, est: float, requests_this_job: int) -> None:
    lim = limits(db)
    if requests_this_job >= lim["dataforseo_max_requests_per_job"]:
        raise BudgetExceeded("per-job DataForSEO request limit reached")
    sp = spent(db, "dataforseo")
    # use the larger of reported / estimated so an estimate never hides real spend
    day = max(sp["day_reported"], sp["day_estimated"])
    month = max(sp["month_reported"], sp["month_estimated"])
    if day + est > lim["dataforseo_daily_usd"]:
        raise BudgetExceeded(f"daily DataForSEO limit ${lim['dataforseo_daily_usd']:.2f} would be exceeded")
    if month + est > lim["dataforseo_monthly_usd"]:
        raise BudgetExceeded(f"monthly DataForSEO limit ${lim['dataforseo_monthly_usd']:.2f} would be exceeded")


def check_anthropic(db: Session, calls_this_job: int) -> None:
    lim = limits(db)
    if calls_this_job >= lim["anthropic_max_calls_per_job"]:
        raise BudgetExceeded("per-job Anthropic call limit reached")
    if spent(db, "anthropic")["day_calls"] >= lim["anthropic_daily_calls"]:
        raise BudgetExceeded("daily Anthropic call limit reached")


def record(db: Session, provider: str, *, kind: str, amount: float | Decimal = 0, calls: int = 1,
           job_run_id: int | None = None, input_tokens: int | None = None, output_tokens: int | None = None,
           detail: dict | None = None) -> None:
    db.add(SpendLedger(provider=provider, kind=kind, amount_usd=Decimal(str(amount)), calls=calls, job_run_id=job_run_id,
                       input_tokens=input_tokens, output_tokens=output_tokens, detail=detail or {}))
    db.flush()
