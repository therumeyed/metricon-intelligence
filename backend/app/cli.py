"""Command line entrypoints (used by Render cron jobs and for local operation).

  python -m app.cli migrate                     apply database migrations
  python -m app.cli seed                        load brands/regions/themes/sources/news queries (creates missing rows only)
  python -m app.cli create-user EMAIL --role admin
  python -m app.cli import-gsc FILE.xlsx --period-start 2026-01-01 --period-end 2026-10-10
  python -m app.cli run JOB [--params '{"kinds":["offer_listing"]}']
  python -m app.cli dispatch                    run every scheduled job that is due (Australia/Sydney time); safe to run hourly
  python -m app.cli daily | weekly              run that pipeline now, in dependency order
  python -m app.cli trends-capability [--cities]
  python -m app.cli jobs                        show recent job runs
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys

from . import jobs as _jobs  # noqa: F401  (registers job functions)
from .db import session_scope
from .services import jobs


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed")
    cu = sub.add_parser("create-user")
    cu.add_argument("email")
    cu.add_argument("--role", default="viewer", choices=["viewer", "admin"])
    cu.add_argument("--name")
    cu.add_argument("--password")
    ig = sub.add_parser("import-gsc")
    ig.add_argument("file")
    ig.add_argument("--period-start")
    ig.add_argument("--period-end")
    ig.add_argument("--note", default="Reported by the client; the export has no date column.")
    r = sub.add_parser("run")
    r.add_argument("job")
    r.add_argument("--params", default="{}")
    sub.add_parser("dispatch")
    sub.add_parser("daily")
    sub.add_parser("weekly")
    tc = sub.add_parser("trends-capability")
    tc.add_argument("--cities", action="store_true")
    sub.add_parser("jobs")
    a = ap.parse_args(argv)

    if a.cmd == "migrate":
        from alembic import command
        from alembic.config import Config
        from pathlib import Path

        cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
        command.upgrade(cfg, "head")
        print("migrated")
    elif a.cmd == "seed":
        from .seed import seed_all

        with session_scope() as db:
            print(json.dumps(seed_all(db)))
    elif a.cmd == "create-user":
        from . import auth

        pw = a.password or getpass.getpass("Password (min 10 chars): ")
        with session_scope() as db:
            u = auth.create_user(db, a.email, pw, a.role, a.name)
            print("created", u.email, u.role)
    elif a.cmd == "import-gsc":
        from datetime import date

        from .services import gsc

        with session_scope() as db:
            res = gsc.import_workbook(db, a.file, period_start=date.fromisoformat(a.period_start) if a.period_start else None,
                                      period_end=date.fromisoformat(a.period_end) if a.period_end else None, period_note=a.note)
            if res["created"]:
                res["terms"] = gsc.propose_representative_terms(db, res["import_id"])
            print(json.dumps(res))
    elif a.cmd == "run":
        print(json.dumps(jobs.run_now(a.job, json.loads(a.params), trigger="manual"), default=str, indent=1))
    elif a.cmd == "dispatch":
        for r_ in jobs.dispatch():
            print(json.dumps(r_, default=str)[:600])
    elif a.cmd in ("daily", "weekly"):
        names = jobs.DAILY_PIPELINE if a.cmd == "daily" else jobs.WEEKLY_PIPELINE
        failed = False
        for n in names:  # each step is isolated: a failure never stops the next collector
            res = jobs.run_now(n, trigger="manual")
            print(n, res.get("status"), json.dumps({k: v for k, v in res.items() if k in ("errors", "not_connected")}, default=str)[:300])
            failed |= res.get("status") == "failed"
        return 1 if failed else 0
    elif a.cmd == "trends-capability":
        print(json.dumps(jobs.run_now("trends_capability", {"cities": a.cities}, trigger="manual"), default=str, indent=1))
    elif a.cmd == "jobs":
        from sqlalchemy import select

        from .models import JobRun

        with session_scope() as db:
            for r_ in db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(25)):
                print(r_.id, r_.job_name, r_.status, r_.trigger, r_.finished_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
