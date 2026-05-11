"""Auth routes: signup, login, logout, me."""
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from sqlalchemy.orm import Session
from sqlalchemy import select

from .auth import (
    hash_password, verify_password, make_session_token,
    get_current_user, get_current_user_optional,
)
from .config import settings
from .db import get_db, User
from .schemas import SignupRequest, LoginRequest, UserOut


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(resp: Response, user_id: int) -> None:
    token = make_session_token(user_id)
    resp.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


@router.post("/signup", response_model=UserOut)
def signup(body: SignupRequest, response: Response, db: Session = Depends(get_db)):
    if not settings.allow_signup:
        raise HTTPException(status_code=403, detail="signup is disabled on this instance")
    existing = db.scalar(select(User).where(User.email == body.email.lower()))
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")
    # First user becomes admin automatically.
    is_first = db.scalar(select(User.id).limit(1)) is None
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=1 if is_first else 0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, user.id)
    return UserOut.from_user(user)


@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    _set_session_cookie(response, user.id)
    return UserOut.from_user(user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if user is None:
        return {"authenticated": False, "allow_signup": settings.allow_signup}
    return {"authenticated": True, "user": UserOut.from_user(user).model_dump(mode="json")}
