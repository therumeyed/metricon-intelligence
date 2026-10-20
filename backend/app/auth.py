"""Session auth with admin-created accounts and two roles (viewer, admin). No public registration."""
from __future__ import annotations

import time
from collections import defaultdict, deque

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import AppUser

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "metricon-ui"
_attempts: dict[str, deque] = defaultdict(deque)
MAX_ATTEMPTS, WINDOW_S = 8, 900


def hash_password(pw: str) -> str:
    if len(pw) < 10:
        raise ValueError("password must be at least 10 characters")
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def throttle(key: str) -> None:
    now = time.time()
    q = _attempts[key]
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    if len(q) >= MAX_ATTEMPTS:
        raise HTTPException(429, "Too many sign-in attempts. Try again in a few minutes.")


def record_failure(key: str) -> None:
    _attempts[key].append(time.time())


def clear_failures(key: str) -> None:
    _attempts.pop(key, None)


def csrf_guard(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Missing request header")


def open_access_user() -> AppUser:
    """Stand-in identity when login is switched off. Transient (never saved); audit rows record user_id=NULL."""
    return AppUser(id=None, email="open-access", name="Open access", role="admin", active=True, password_hash="")


def viewer_open_user() -> AppUser:
    return AppUser(id=None, email="open-access", name="Open access", role="viewer", active=True, password_hash="")


def admin_unlock_enabled() -> bool:
    from .config import get_settings

    s = get_settings()
    return bool(s.admin_password) and not s.require_login


def check_admin_password(pw: str) -> bool:
    import hmac

    from .config import get_settings

    expected = get_settings().admin_password
    return bool(expected) and hmac.compare_digest(pw.encode(), expected.encode())


def current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    from .config import get_settings

    if admin_unlock_enabled():  # everyone can read; only a browser that entered the admin password acts as administrator
        return open_access_user() if request.session.get("admin_unlocked") is True else viewer_open_user()
    if not get_settings().require_login:
        return open_access_user()
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "Sign in required")
    user = db.get(AppUser, uid)
    if user is None or not user.active:
        request.session.clear()
        raise HTTPException(401, "Sign in required")
    return user


def require_admin(user: AppUser = Depends(current_user)) -> AppUser:
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


def create_user(db: Session, email: str, password: str, role: str = "viewer", name: str | None = None) -> AppUser:
    email = email.strip().lower()
    if role not in ("viewer", "admin"):
        raise ValueError("role must be viewer or admin")
    if db.scalar(select(AppUser).where(AppUser.email == email)):
        raise ValueError("a user with that email already exists")
    u = AppUser(email=email, name=name, password_hash=hash_password(password), role=role)
    db.add(u)
    db.flush()
    return u


def ensure_seed_admin(db: Session) -> bool:
    from .config import get_settings

    s = get_settings()
    if s.seed_admin_email and s.seed_admin_password and not db.scalar(select(AppUser).where(AppUser.role == "admin")):
        create_user(db, s.seed_admin_email, s.seed_admin_password, "admin", "Administrator")
        return True
    return False
