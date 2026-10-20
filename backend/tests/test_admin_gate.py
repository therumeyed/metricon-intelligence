"""Admin password gate: reading stays open, admin needs the password. The password here is a TEST value."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

H = {"X-Requested-With": "metricon-ui"}


@pytest.fixture
def gated(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("SESSION_SECRET", "a-long-test-secret-value")
    get_settings.cache_clear()
    from app.main import create_app

    yield TestClient(create_app())
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    monkeypatch.delenv("ADMIN_PASSWORD")
    get_settings.cache_clear()


def test_reading_is_open_but_admin_is_locked(gated):
    assert gated.get("/api/offers").status_code == 200 and gated.get("/api/analysis").status_code == 200
    me = gated.get("/api/auth/me").json()
    assert me["role"] == "viewer" and me["admin_gate"] is True
    assert gated.get("/api/admin/jobs").status_code == 403
    assert gated.post("/api/admin/jobs/run", json={"name": "check_macro"}, headers=H).status_code == 403


def test_wrong_password_is_refused_and_right_one_unlocks_this_browser_only(gated, seeded):
    assert gated.post("/api/auth/admin-unlock", json={"password": "nope"}, headers=H).status_code == 401
    assert gated.get("/api/admin/jobs").status_code == 403
    assert gated.post("/api/auth/admin-unlock", json={"password": "test-admin-pass"}).status_code == 403  # CSRF header still required
    assert gated.post("/api/auth/admin-unlock", json={"password": "test-admin-pass"}, headers=H).status_code == 200
    assert gated.get("/api/auth/me").json()["role"] == "admin" and gated.get("/api/admin/jobs").status_code == 200
    other = TestClient(gated.app)  # a different browser has no cookie
    assert other.get("/api/admin/jobs").status_code == 403
    assert gated.post("/api/auth/admin-lock", headers=H).status_code == 200
    assert gated.get("/api/admin/jobs").status_code == 403


def test_repeated_wrong_guesses_are_throttled(gated):
    from app import auth

    auth._attempts.clear()
    codes = [gated.post("/api/auth/admin-unlock", json={"password": f"x{i}"}, headers=H).status_code for i in range(10)]
    assert codes[:8] == [401] * 8 and codes[-1] == 429
    auth._attempts.clear()


def test_without_the_variable_nothing_changes(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()
    from app.main import create_app

    c = TestClient(create_app())
    assert c.get("/api/auth/me").json()["admin_gate"] is False and c.get("/api/admin/jobs").status_code == 200
    assert c.post("/api/auth/admin-unlock", json={"password": "x"}, headers=H).status_code == 400
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
