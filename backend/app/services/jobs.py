"""Database-backed job runner with locks, heartbeats, checkpoints and a due-date dispatcher.

* One advisory lock per job name (Postgres session lock) stops overlapping runs; if a process dies the lock
  disappears with its connection.
* Each job opens short sessions and commits as it goes, so completed work survives a later failure.
* The dispatcher decides what is due using Australia/Sydney business time, so Render's UTC cron and daylight
  saving never shift a "6am Sydney" job: the cron simply fires hourly and the dispatcher checks the slot.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_engine, session_factory, session_scope
from ..models import JobRun
from ..timeutil import app_tz, now_utc

log = logging.getLogger("jobs")

MAX_RUNTIME = timedelta(hours=2)


@dataclass
class JobContext:
    run_id: int
    name: str
    params: dict
    checkpoint: dict = field(default_factory=dict)

    def db(self) -> Session:
        return session_factory()()

    def save_checkpoint(self, **kv: Any) -> None:
        self.checkpoint.update(kv)
        with session_scope() as s:
            run = s.get(JobRun, self.run_id)
            run.checkpoint = dict(self.checkpoint)
            run.heartbeat_at = now_utc()

    def heartbeat(self) -> None:
        with session_scope() as s:
            s.get(JobRun, self.run_id).heartbeat_at = now_utc()

    def deadline_exceeded(self) -> bool:
        return (now_utc() - self._started) > MAX_RUNTIME

    _started: datetime = field(default_factory=now_utc)


JobFn = Callable[[JobContext], dict]
REGISTRY: dict[str, JobFn] = {}


def job(name: str):
    def deco(fn: JobFn) -> JobFn:
        REGISTRY[name] = fn
        return fn
    return deco


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(f"metricon-job:{name}".encode()).digest()[:7], "big")


def enqueue(name: str, params: dict | None = None, *, trigger: str = "manual", user_id: int | None = None) -> int:
    if name not in REGISTRY:
        raise KeyError(f"unknown job {name!r}")
    with session_scope() as s:
        run = JobRun(job_name=name, params=params or {}, trigger=trigger, requested_by=user_id, status="queued")
        s.add(run)
        s.flush()
        return run.id


def execute(run_id: int) -> dict:
    """Run a queued JobRun now (in this process). Safe to call from a thread or a CLI."""
    with session_scope() as s:
        run = s.get(JobRun, run_id)
        if run is None:
            raise KeyError(run_id)
        name, params = run.job_name, dict(run.params or {})
        checkpoint = dict(run.checkpoint or {})
    fn = REGISTRY[name]
    conn = get_engine().connect()
    got = conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": _lock_key(name)}).scalar()
    try:
        if not got:
            with session_scope() as s:
                r = s.get(JobRun, run_id)
                r.status, r.finished_at, r.error = "skipped", now_utc(), "another run of this job holds the lock"
            return {"status": "skipped"}
        with session_scope() as s:
            r = s.get(JobRun, run_id)
            r.status, r.started_at, r.heartbeat_at, r.attempts = "running", now_utc(), now_utc(), (r.attempts or 0) + 1
        ctx = JobContext(run_id=run_id, name=name, params=params, checkpoint=checkpoint)
        try:
            result = fn(ctx) or {}
            status = result.pop("_status", None) or ("partial" if result.get("errors") else "succeeded")
            from . import llm as _llm

            result["connections"] = {"ai": _llm.available(), "trends": get_settings().dataforseo_ready}  # what THIS process (web or cron) can see
            err = None
        except Exception as e:  # noqa: BLE001 - a job failure is recorded, never raised into the scheduler
            log.exception("job %s failed", name)
            result, status, err = {}, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}"
        with session_scope() as s:
            r = s.get(JobRun, run_id)
            r.status, r.finished_at, r.result, r.error = status, now_utc(), result, err
        return {"status": status, **result}
    finally:
        if got:
            conn.execute(text("select pg_advisory_unlock(:k)"), {"k": _lock_key(name)})
        conn.close()


def execute_in_thread(run_id: int) -> None:
    threading.Thread(target=execute, args=(run_id,), daemon=True, name=f"job-{run_id}").start()


def run_now(name: str, params: dict | None = None, *, trigger: str = "schedule", user_id: int | None = None) -> dict:
    run_id = enqueue(name, params, trigger=trigger, user_id=user_id)
    out = execute(run_id)
    out["run_id"] = run_id
    return out


def retry(run_id: int, user_id: int | None = None) -> int:
    with session_scope() as s:
        old = s.get(JobRun, run_id)
        name, params = old.job_name, dict(old.params or {})
    return enqueue(name, params, trigger="retry", user_id=user_id)


def reap_stale() -> int:
    """Mark runs whose process vanished (no heartbeat for 15 minutes) as failed so they can be retried."""
    cutoff = now_utc() - timedelta(minutes=15)
    n = 0
    with session_scope() as s:
        for r in s.scalars(select(JobRun).where(JobRun.status == "running", JobRun.heartbeat_at < cutoff)):
            r.status, r.finished_at, r.error = "failed", now_utc(), "worker stopped without finishing (stale heartbeat)"
            n += 1
    return n


# --------------------------------------------------------------------------- schedule
@dataclass(frozen=True)
class Schedule:
    job: str
    cadence: str  # daily | weekly
    hour: int  # Australia/Sydney local hour
    weekday: int | None = None  # 0=Monday for weekly


DAILY_PIPELINE = ["collect_sources", "collect_search", "extract_offers", "detect_events", "analyse_activity", "collect_news", "check_macro"]
WEEKLY_PIPELINE = ["refresh_trends", "discover_related", "generate_briefs"]
SCHEDULES = [
    Schedule("collect_sources", "daily", 5),
    Schedule("collect_search", "daily", 5),
    Schedule("extract_offers", "daily", 5),
    Schedule("detect_events", "daily", 5),
    Schedule("analyse_activity", "daily", 5),
    Schedule("collect_news", "daily", 6),
    Schedule("check_macro", "daily", 6),
    Schedule("refresh_trends", "weekly", 4, 0),
    Schedule("discover_related", "weekly", 4, 0),
    Schedule("generate_briefs", "weekly", 8, 0),
]


def latest_slot(sch: Schedule, now: datetime | None = None) -> datetime:
    """Most recent scheduled instant (UTC) at or before now, computed in Australia/Sydney time."""
    local = (now or now_utc()).astimezone(app_tz())
    slot = local.replace(hour=sch.hour, minute=0, second=0, microsecond=0)
    if sch.cadence == "daily":
        if slot > local:
            slot -= timedelta(days=1)
    else:
        slot -= timedelta(days=(slot.weekday() - (sch.weekday or 0)) % 7)
        if slot > local:
            slot -= timedelta(days=7)
    return slot.astimezone(timezone.utc)


def due_jobs(now: datetime | None = None) -> list[str]:
    now = now or now_utc()
    due = []
    with session_scope() as s:
        for sch in SCHEDULES:
            slot = latest_slot(sch, now)
            last = s.scalar(select(JobRun.queued_at).where(
                JobRun.job_name == sch.job, JobRun.trigger == "schedule", JobRun.status.in_(["succeeded", "partial", "running", "queued"]),
                JobRun.queued_at >= slot).limit(1))
            failed = s.scalar(select(func.count()).select_from(JobRun).where(
                JobRun.job_name == sch.job, JobRun.trigger == "schedule", JobRun.status == "failed", JobRun.queued_at >= slot)) or 0
            if last is None and failed < 3:  # bounded retries per slot; admins can retry manually
                due.append(sch.job)
    return due


def dispatch(now: datetime | None = None) -> list[dict]:
    """Run every due scheduled job, in dependency order, plus any queued manual jobs. Each job is isolated."""
    reap_stale()
    results = []
    order = {n: i for i, n in enumerate(DAILY_PIPELINE + WEEKLY_PIPELINE)}
    for name in sorted(due_jobs(now), key=lambda n: order.get(n, 99)):
        results.append({"job": name, **run_now(name, trigger="schedule")})
    with session_scope() as s:
        queued = [r.id for r in s.scalars(select(JobRun).where(JobRun.status == "queued").order_by(JobRun.queued_at))]
    for rid in queued:
        results.append({"job": "queued", "run_id": rid, **execute(rid)})
    return results
