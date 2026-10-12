"""Regression: the web process must know every job, not just the CLI (Admin showed no buttons and 'unknown job' errors)."""
import subprocess
import sys

EXPECTED = {"collect_sources", "extract_offers", "detect_events", "collect_news", "check_macro", "refresh_trends", "discover_related", "generate_briefs", "trends_capability", "import_gsc", "classify_gsc"}


def test_importing_only_the_web_app_registers_all_jobs():
    out = subprocess.run([sys.executable, "-c", "import app.main; from app.services.jobs import REGISTRY; print(sorted(REGISTRY))"],
                         capture_output=True, text=True, cwd=__file__.rsplit("/tests/", 1)[0], timeout=60)
    assert out.returncode == 0, out.stderr
    assert EXPECTED <= set(eval(out.stdout.strip()))


def test_admin_job_list_and_run_work_through_the_web_api(seeded, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    get_settings.cache_clear()
    from app.main import create_app

    c = TestClient(create_app())
    h = {"X-Requested-With": "metricon-ui"}
    assert EXPECTED - {"import_gsc"} <= set(c.get("/api/admin/jobs").json()["available"])
    assert c.post("/api/admin/jobs/run", json={"name": "not_a_job"}, headers=h).status_code == 400
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
