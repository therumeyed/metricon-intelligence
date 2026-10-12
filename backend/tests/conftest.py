"""Test harness. Runs against a real PostgreSQL database (default: local metricon_test) created from the Alembic migration.
All fixtures here are synthetic and labelled as such - they never touch live market tables outside this test database."""
from __future__ import annotations

import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://metricon:metricon@localhost:5433/metricon_test")
os.environ["SESSION_SECRET"] = "test-secret"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DATAFORSEO_LOGIN", None)
os.environ.pop("DATAFORSEO_PASSWORD", None)
os.environ["CLAUDE_MODEL"] = ""
os.environ["SEED_ADMIN_EMAIL"] = ""

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine, session_factory

get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("drop schema public cascade; create schema public;"))
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")  # proves the migration builds the schema
    yield


@pytest.fixture(autouse=True)
def _clean(_schema):
    eng = get_engine()
    with eng.begin() as c:
        names = [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version'"))]
        c.execute(text("truncate table " + ", ".join(f'"{n}"' for n in names) + " restart identity cascade"))
    from app.services import fetcher

    fetcher._robots.clear()
    yield


@pytest.fixture
def db():
    s = session_factory()()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def seeded(db):
    from app.seed import seed_all

    seed_all(db)
    db.commit()
    return db
