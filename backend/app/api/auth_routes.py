from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_db
from ..models import AppUser
from ..timeutil import now_utc

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login", dependencies=[Depends(auth.csrf_guard)])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else '?'}|{body.email.strip().lower()}"
    auth.throttle(key)
    user = db.scalar(select(AppUser).where(AppUser.email == body.email.strip().lower()))
    if not user or not user.active or not auth.verify_password(body.password, user.password_hash):
        auth.record_failure(key)
        raise HTTPException(401, "Email or password is incorrect")
    auth.clear_failures(key)
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login_at = now_utc()
    db.commit()
    return {"email": user.email, "name": user.name, "role": user.role}


class UnlockIn(BaseModel):
    password: str


@router.post("/admin-unlock", dependencies=[Depends(auth.csrf_guard)])
def admin_unlock(body: UnlockIn, request: Request):
    if not auth.admin_unlock_enabled():
        raise HTTPException(400, "No admin password is set on this service")
    key = f"admin|{request.client.host if request.client else '?'}"
    auth.throttle(key)
    if not auth.check_admin_password(body.password):
        auth.record_failure(key)
        raise HTTPException(401, "Wrong password")
    auth.clear_failures(key)
    request.session["admin_unlocked"] = True
    return {"role": "admin"}


@router.post("/admin-lock", dependencies=[Depends(auth.csrf_guard)])
def admin_lock(request: Request):
    request.session.pop("admin_unlocked", None)
    return {"role": "viewer"}


@router.post("/logout", dependencies=[Depends(auth.csrf_guard)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user: AppUser = Depends(auth.current_user)):
    from ..config import get_settings

    return {"email": user.email, "name": user.name, "role": user.role, "login_required": get_settings().require_login, "admin_gate": auth.admin_unlock_enabled()}
