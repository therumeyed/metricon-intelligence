"""With REQUIRE_LOGIN off (the default) there is no sign-in: everything is reachable and actions are attributed to nobody."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog

H = {"X-Requested-With": "metricon-ui"}


@pytest.fixture
def open_client(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    get_settings.cache_clear()
    from app.main import create_app

    yield TestClient(create_app())
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()


def test_no_sign_in_needed_for_reader_export_or_admin_routes(open_client):
    me = open_client.get("/api/auth/me").json()
    assert me["role"] == "admin" and me["login_required"] is False
    for path in ["/api/meta", "/api/offers", "/api/offers/export.csv", "/api/brief", "/api/admin/jobs", "/api/admin/sources", "/api/admin/brands"]:
        assert open_client.get(path).status_code == 200, path


def test_admin_actions_still_work_and_are_audited_without_a_user(open_client, seeded):
    r = open_client.post("/api/admin/brands", json={"name": "Test Builder", "states": ["VIC"]}, headers=H)
    assert r.status_code == 200
    row = seeded.scalars(select(AuditLog).where(AuditLog.action == "brand.create")).one()
    assert row.user_id is None
    assert open_client.post("/api/admin/brands", json={"name": "Nope"}).status_code == 403  # the CSRF header is still required


def test_login_comes_back_when_switched_on(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    from app.main import create_app

    c = TestClient(create_app())
    assert c.get("/api/offers").status_code == 401
