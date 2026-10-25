import hashlib
import hmac
import secrets
from datetime import timezone, timedelta
from fastapi import Depends, HTTPException, Request
from .db import SessionLocal, SQLITE
from .models import Session, User, now

COOKIE_NAME = "credit_session"


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def get_db(request: Request):
    db = SessionLocal()
    try:
        if SQLITE:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_user(request: Request, db=Depends(get_db, scope="function")):
    token = request.cookies.get(COOKIE_NAME, "")
    session = db.get(Session, hashlib.sha256(token.encode()).hexdigest()) if token else None
    if (
        session is None
        or utc(session.expires_at) <= now()
        or session.last_seen_at is not None
        and utc(session.last_seen_at) + timedelta(minutes=30) <= now()
    ):
        raise HTTPException(401, "Войдите в кабинет")
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(401, "Сессия недействительна")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(csrf, session.csrf_token):
            raise HTTPException(403, "Защитный токен устарел. Обновите страницу")
    session.last_seen_at = now()
    request.state.session = session
    return user


def require_bank(user=Depends(get_user)):
    if user.role != "bank":
        raise HTTPException(403, "Доступно только сотруднику банка")
    return user


def require_entrepreneur(user=Depends(get_user)):
    if user.role != "entrepreneur":
        raise HTTPException(403, "Доступно только предпринимателю")
    return user


def user_dict(user):
    return {key: getattr(user, key) for key in ("id", "email", "name", "role", "company_id")}
