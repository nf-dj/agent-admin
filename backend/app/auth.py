"""Auth helpers: bcrypt + signed session cookies."""
import bcrypt
from itsdangerous import TimestampSigner, BadSignature, SignatureExpired
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from .config import settings
from .db import User, get_db


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _signer() -> TimestampSigner:
    return TimestampSigner(settings.secret_key)


def make_session_token(user_id: int) -> str:
    return _signer().sign(str(user_id).encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> int | None:
    try:
        raw = _signer().unsign(token, max_age=settings.session_max_age_seconds)
        return int(raw.decode("utf-8"))
    except (BadSignature, SignatureExpired, ValueError):
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    tok = request.cookies.get(settings.cookie_name)
    if not tok:
        raise HTTPException(status_code=401, detail="not authenticated")
    uid = parse_session_token(tok)
    if uid is None:
        raise HTTPException(status_code=401, detail="invalid session")
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    tok = request.cookies.get(settings.cookie_name)
    if not tok:
        return None
    uid = parse_session_token(tok)
    if uid is None:
        return None
    return db.get(User, uid)
