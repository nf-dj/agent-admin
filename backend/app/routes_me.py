"""Routes for the logged-in user (`/api/me/*`).

Currently just provisions/returns the user's web-chat Matrix credentials.
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import SessionLocal, User, WebMatrixCredential, UserApiKey
from .matrix_admin import matrix_admin, MatrixError
from .crypto import make_preview
from .auth_sync import sync_user_provider
from .schemas import (
    WebMatrixCredsOut, UserOut, UserSettingsUpdate,
    UserApiKeyOut, UserApiKeySet, ALLOWED_KEY_PROVIDERS,
)


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/me", tags=["me"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_creds(db: Session, user: User) -> WebMatrixCredential:
    """Return existing web-Matrix creds for this user, lazily provisioning if missing."""
    existing = db.query(WebMatrixCredential).filter(
        WebMatrixCredential.user_id == user.id
    ).one_or_none()
    if existing is not None:
        # Cheap sanity check: ask /whoami to confirm token still works.
        # If it fails (e.g. admin nuked the device), re-login with the stored
        # password to mint a fresh token. We don't strictly need this every
        # request, but it's cheap and keeps `sessionStorage`-backed clients
        # working across long pauses.
        try:
            matrix_admin._request(
                "GET", "/_matrix/client/v3/account/whoami",
                token=existing.access_token, timeout=5)
            return existing
        except MatrixError as e:
            if e.status in (401, 403):
                log.info("Refreshing matrix token for user %s", user.id)
                login = matrix_admin.login_user(existing.matrix_localpart,
                                                 existing.matrix_password)
                existing.access_token = login["access_token"]
                existing.device_id = login.get("device_id", existing.device_id)
                db.add(existing)
                db.commit()
                db.refresh(existing)
                return existing
            raise

    if not matrix_admin.enabled:
        raise HTTPException(status_code=503,
                            detail="Matrix integration is not configured on this server")

    display_name = user.display_name or user.email.split("@")[0]
    try:
        provisioned = matrix_admin.create_web_user(
            f"u{user.id}", display_name=display_name)
    except MatrixError as e:
        log.exception("Failed to provision Matrix user for app user %s", user.id)
        raise HTTPException(status_code=502, detail=f"Matrix provisioning failed: {e}")

    cred = WebMatrixCredential(
        user_id=user.id,
        matrix_user_id=provisioned.user_id,
        matrix_localpart=provisioned.localpart,
        homeserver=provisioned.homeserver,
        access_token=provisioned.access_token,
        device_id=provisioned.device_id,
        matrix_password=provisioned.password,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


@router.get("/matrix-creds", response_model=WebMatrixCredsOut)
def matrix_creds(current: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    cred = _ensure_creds(db, current)
    return WebMatrixCredsOut(
        matrix_user_id=cred.matrix_user_id,
        homeserver=cred.homeserver,
        access_token=cred.access_token,
        device_id=cred.device_id,
    )


# --- User settings -----------------------------------------------------------

@router.get("/settings", response_model=UserOut)
def get_settings(current: User = Depends(get_current_user)):
    """Return the current user's profile + settings.

    Re-uses ``UserOut`` so the frontend stays in sync with the auth payload.
    """
    return UserOut.from_user(current)


@router.patch("/settings", response_model=UserOut)
def update_settings(body: UserSettingsUpdate,
                    current: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Partially update the current user's profile/settings.

    ``company_prefix`` becomes the agent-id prefix used for *new* bots
    (existing bot ids are not renamed — they're baked into OpenClaw).
    Pass an empty string to clear it and fall back to ``u<id>-``.
    """
    # Re-attach `current` to this session (FastAPI dependency may have used a different one).
    user = db.get(User, current.id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if body.display_name is not None:
        dn = body.display_name.strip()
        user.display_name = dn or None

    if body.company_prefix is not None:
        # Empty string from the validator means "clear it".
        user.company_prefix = body.company_prefix or None

    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.from_user(user)


# --- API keys -----------------------------------------------------------------
# Stored encrypted at rest (see ``crypto.py``). The plaintext is never returned
# by any read endpoint — only a short preview (e.g. "sk-1…2xyz").

def _validate_provider(provider: str) -> str:
    p = provider.strip().lower()
    if p not in ALLOWED_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {provider!r}. Allowed: "
                   f"{sorted(ALLOWED_KEY_PROVIDERS)}",
        )
    return p


@router.get("/api-keys", response_model=list[UserApiKeyOut])
def list_api_keys(current: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """List all providers the user has stored a key for.

    Returns one row per known provider — with ``has_key=false`` for those
    that aren't set — so the UI can render the full list in a stable order
    without a second round-trip.
    """
    rows = {
        r.provider: r for r in db.query(UserApiKey)
        .filter(UserApiKey.user_id == current.id)
        .all()
    }
    out: list[UserApiKeyOut] = []
    for provider in sorted(ALLOWED_KEY_PROVIDERS):
        r = rows.get(provider)
        if r is None:
            out.append(UserApiKeyOut(provider=provider, has_key=False))
        else:
            out.append(UserApiKeyOut(
                provider=provider, has_key=True,
                preview=r.key_preview, updated_at=r.updated_at,
            ))
    return out


@router.put("/api-keys/{provider}", response_model=UserApiKeyOut)
def set_api_key(provider: str, body: UserApiKeySet,
                current: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Set (or replace) the API key for a provider."""
    provider = _validate_provider(provider)
    existing = db.query(UserApiKey).filter(
        UserApiKey.user_id == current.id,
        UserApiKey.provider == provider,
    ).one_or_none()

    key_preview = make_preview(body.api_key)

    if existing is None:
        row = UserApiKey(
            user_id=current.id, provider=provider,
            api_key=body.api_key, key_preview=key_preview,
        )
        db.add(row)
    else:
        existing.api_key = body.api_key
        existing.key_preview = key_preview
        row = existing

    db.commit()
    db.refresh(row)

    # Push the new key out to every agent owned by this user. Agents with
    # their own per-agent override are still re-synced — the override just
    # shadows the user key, so the on-disk state stays consistent.
    sync_user_provider(db, current.id, provider)

    return UserApiKeyOut(
        provider=row.provider, has_key=True,
        preview=row.key_preview, updated_at=row.updated_at,
    )


@router.delete("/api-keys/{provider}", response_model=UserApiKeyOut)
def delete_api_key(provider: str,
                   current: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Remove the stored API key for a provider. Idempotent."""
    provider = _validate_provider(provider)
    existing = db.query(UserApiKey).filter(
        UserApiKey.user_id == current.id,
        UserApiKey.provider == provider,
    ).one_or_none()
    if existing is not None:
        db.delete(existing)
        db.commit()
        # Re-sync this provider across all owned agents — agents without
        # a per-agent override will now have the profile cleared on disk.
        sync_user_provider(db, current.id, provider)
    return UserApiKeyOut(provider=provider, has_key=False)
