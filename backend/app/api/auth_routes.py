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


@router.post("/logout", dependencies=[Depends(auth.csrf_guard)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user: AppUser = Depends(auth.current_user)):
    return {"email": user.email, "name": user.name, "role": user.role}
