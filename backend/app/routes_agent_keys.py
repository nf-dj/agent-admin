"""Per-agent API key overrides.

These keys override the owner's user-level saved keys (see ``routes_me.py``).
Endpoints are owner-only — members can't read or set agent keys. The
plaintext is encrypted at rest with the same Fernet helper used for user
keys and is never returned to clients.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import SessionLocal, User, AgentApiKey, UserApiKey
from .crypto import make_preview
from .auth_sync import sync_agent_provider
from .permissions import require_owner
from .schemas import (
    AgentApiKeyOut, AgentApiKeySet, ALLOWED_KEY_PROVIDERS,
)


router = APIRouter(prefix="/api/agents", tags=["agent-api-keys"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _validate_provider(provider: str) -> str:
    p = provider.strip().lower()
    if p not in ALLOWED_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {provider!r}. Allowed: "
                   f"{sorted(ALLOWED_KEY_PROVIDERS)}",
        )
    return p


def _build_out(provider: str,
               override: AgentApiKey | None,
               user_saved: UserApiKey | None) -> AgentApiKeyOut:
    """Combine override + user-saved info into a single response row."""
    return AgentApiKeyOut(
        provider=provider,
        has_override=override is not None,
        override_preview=override.key_preview if override else None,
        override_updated_at=override.updated_at if override else None,
        user_has_saved=user_saved is not None,
        user_saved_preview=user_saved.key_preview if user_saved else None,
    )


@router.get("/{agent_id}/api-keys", response_model=list[AgentApiKeyOut])
def list_agent_api_keys(agent_id: int,
                       current: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """List every supported provider with per-agent override + user-saved status.

    Owner-only. Returns a stable row per provider (even ones with no override
    and no user-saved key) so the UI can render the full list without a
    second round-trip.
    """
    agent = require_owner(db, current, agent_id)

    overrides = {
        r.provider: r for r in db.query(AgentApiKey)
        .filter(AgentApiKey.agent_id == agent.id).all()
    }
    user_keys = {
        r.provider: r for r in db.query(UserApiKey)
        .filter(UserApiKey.user_id == current.id).all()
    }

    return [
        _build_out(p, overrides.get(p), user_keys.get(p))
        for p in sorted(ALLOWED_KEY_PROVIDERS)
    ]


@router.put("/{agent_id}/api-keys/{provider}", response_model=AgentApiKeyOut)
def set_agent_api_key(agent_id: int, provider: str, body: AgentApiKeySet,
                     current: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Set or replace the per-agent override for a provider. Owner-only."""
    agent = require_owner(db, current, agent_id)
    provider = _validate_provider(provider)

    existing = db.query(AgentApiKey).filter(
        AgentApiKey.agent_id == agent.id,
        AgentApiKey.provider == provider,
    ).one_or_none()

    key_preview = make_preview(body.api_key)

    if existing is None:
        row = AgentApiKey(
            agent_id=agent.id, provider=provider,
            api_key=body.api_key, key_preview=key_preview,
        )
        db.add(row)
    else:
        existing.api_key = body.api_key
        existing.key_preview = key_preview
        row = existing

    db.commit()
    db.refresh(row)

    # Push to disk: this agent now has its own override that should win
    # over any user-level key for the same provider.
    sync_agent_provider(db, agent, provider)

    # Also include the user-saved info so the UI can render consistently.
    user_saved = db.query(UserApiKey).filter(
        UserApiKey.user_id == current.id,
        UserApiKey.provider == provider,
    ).one_or_none()
    return _build_out(provider, row, user_saved)


@router.delete("/{agent_id}/api-keys/{provider}", response_model=AgentApiKeyOut)
def delete_agent_api_key(agent_id: int, provider: str,
                        current: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Remove the per-agent override for a provider. Idempotent. Owner-only.

    Falls back to the user's saved key (if any) when this agent next runs.
    """
    agent = require_owner(db, current, agent_id)
    provider = _validate_provider(provider)

    existing = db.query(AgentApiKey).filter(
        AgentApiKey.agent_id == agent.id,
        AgentApiKey.provider == provider,
    ).one_or_none()
    if existing is not None:
        db.delete(existing)
        db.commit()
        # Re-sync: fall back to the user-level key (if any), or clear the
        # profile entirely if there's nothing to fall back to.
        sync_agent_provider(db, agent, provider)

    user_saved = db.query(UserApiKey).filter(
        UserApiKey.user_id == current.id,
        UserApiKey.provider == provider,
    ).one_or_none()
    return _build_out(provider, None, user_saved)
