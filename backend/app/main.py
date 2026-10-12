from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .api import admin, auth_routes, reader
from .config import get_settings
from .db import session_scope

log = logging.getLogger("app")


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Metricon Market Intelligence", docs_url=None, redoc_url=None, openapi_url=None)
    if s.session_secret in ("dev-only-change-me", "") and s.session_cookie_secure:
        raise RuntimeError("SESSION_SECRET must be set in production")
    app.add_middleware(SessionMiddleware, secret_key=s.session_secret, session_cookie="mi_session", max_age=s.session_max_age_hours * 3600,
                       same_site="lax", https_only=s.session_cookie_secure)

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api") else "public, max-age=300")
        return resp

    app.include_router(auth_routes.router)
    app.include_router(reader.router, dependencies=[Depends(auth.current_user)])
    app.include_router(admin.router, dependencies=[Depends(auth.require_admin), Depends(auth.csrf_guard)])

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    dist = Path(__file__).resolve().parent.parent / "static"
    if not dist.exists():
        dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            f = (dist / path).resolve()
            if path and f.is_file() and dist in f.parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    @app.on_event("startup")
    def _seed_admin():
        try:
            with session_scope() as db:
                if auth.ensure_seed_admin(db):
                    log.warning("Created initial administrator from SEED_ADMIN_EMAIL")
        except Exception:  # noqa: BLE001 - e.g. migrations not applied yet
            log.exception("could not seed admin")

    return app


app = create_app()
