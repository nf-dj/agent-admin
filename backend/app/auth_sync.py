"""Sync SQLite key state → OpenClaw per-agent auth profiles.

OpenClaw stores per-agent credentials in three places:

1. ``~/.openclaw/openclaw.json`` → ``auth.profiles.<profileId>`` :
   the **non-secret registry** the gateway uses to discover profiles
   (just ``{provider, mode}``).
2. ``~/.ocplatform/agents/<agentId>/agent/auth-profiles.json`` :
   the **per-agent secret store**. Each profile here is
   ``{type, provider, key|token}``.
3. ``~/.openclaw/agents/<agentId>/agent/auth-state.json`` :
   the per-agent **order override** (which profile wins when multiple
   exist for the same provider).

Our agent-admin app is the source of truth for keys. This module owns
the writes that propagate SQLite state into those three files.

Resolution rule when syncing one (agent, provider):

    effective = agent_override (AgentApiKey)
             OR owner_user_key (UserApiKey)
             OR (none)  ← profile removed entirely

We use a single profile id per provider for an agent — ``<provider>:agent`` —
so when the effective key changes we just overwrite. No history, no
collision with OpenClaw's own ``<provider>:manual`` / ``:default`` slots.

All file mutations are best-effort: if OpenClaw isn't installed or the
files are locked, we log and move on. The SQLite row is still the
canonical record and a future resync will reconcile.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from .db import Agent, AgentApiKey, UserApiKey, CustomProvider

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Allow override for tests; defaults to David's installation layout.
OCPLATFORM_HOME = Path(os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")))
OCPLATFORM_CONFIG = OCPLATFORM_HOME / "openclaw.json"
AGENTS_DIR = OCPLATFORM_HOME / "agents"

# Stable profile id we own per (agent, provider). Distinct from OpenClaw's
# own ``:manual`` / ``:default`` so we never stomp on hand-managed profiles.
PROFILE_ID_SUFFIX = "agent"


def profile_id_for(provider: str) -> str:
    """Return the canonical auth-profile id this app uses for ``provider``."""
    return f"{provider}:{PROFILE_ID_SUFFIX}"


# ---------------------------------------------------------------------------
# Provider → OpenClaw credential shape
# ---------------------------------------------------------------------------

# Most providers use ``type=api_key`` with a ``key`` field.
# Anthropic-subscription is the odd one — it's an OAuth token from
# ``claude setup-token`` and must be stored as ``type=token``.
#
# (``mode`` in the global registry mirrors the credential type:
#  ``api_key`` for keys, ``token`` for tokens.)
def _credential_for(provider: str, secret: str) -> tuple[dict, str]:
    """Return ``(credential_obj, mode)`` for the per-agent profile file."""
    if provider == "anthropic-subscription":
        # OpenClaw expects this under the real ``anthropic`` provider id
        # with a token credential. Caller should also map provider → "anthropic"
        # before writing — see ``_real_provider``.
        return (
            {"type": "token", "provider": "anthropic", "token": secret},
            "token",
        )
    return (
        {"type": "api_key", "provider": provider, "key": secret},
        "api_key",
    )


def _real_provider(provider: str) -> str:
    """Map our synthetic provider ids to OpenClaw provider ids.

    Only ``anthropic-subscription`` is special — OpenClaw doesn't know
    that name; it's just an Anthropic auth using a token instead of an API
    key. Everything else is 1:1.
    """
    return "anthropic" if provider == "anthropic-subscription" else provider


# ---------------------------------------------------------------------------
# Low-level JSON read/write helpers (atomic via temp-file rename)
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Failed to read %s (%s); using default", path, e)
        return dict(default)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Per-agent file mutations
# ---------------------------------------------------------------------------

def _agent_auth_dir(harness_agent_id: str) -> Path:
    return AGENTS_DIR / harness_agent_id / "agent"


def _write_per_agent_profile(
    harness_agent_id: str, provider: str, secret: str
) -> None:
    """Set our owned profile for ``provider`` in this agent's profile file."""
    real_provider = _real_provider(provider)
    pid = profile_id_for(real_provider)
    credential, _mode = _credential_for(provider, secret)

    path = _agent_auth_dir(harness_agent_id) / "auth-profiles.json"
    store = _read_json(path, {"version": 1, "profiles": {}})
    store.setdefault("profiles", {})[pid] = credential
    _write_json(path, store)


def _remove_per_agent_profile(harness_agent_id: str, provider: str) -> None:
    """Drop our owned profile for ``provider`` in this agent's profile file."""
    real_provider = _real_provider(provider)
    pid = profile_id_for(real_provider)

    path = _agent_auth_dir(harness_agent_id) / "auth-profiles.json"
    if not path.exists():
        return
    store = _read_json(path, {"version": 1, "profiles": {}})
    if store.get("profiles", {}).pop(pid, None) is not None:
        _write_json(path, store)


def _set_per_agent_order(harness_agent_id: str, provider: str, present: bool) -> None:
    """Pin our owned profile first for this provider, or clear the override.

    When ``present`` is True we make sure ``<provider>:agent`` is at the head
    of the per-agent order. When False (we just removed the profile), we
    drop our entry so OpenClaw falls back to its own resolution.
    """
    real_provider = _real_provider(provider)
    pid = profile_id_for(real_provider)

    path = _agent_auth_dir(harness_agent_id) / "auth-state.json"
    state = _read_json(path, {"version": 1, "order": {}})
    order = state.setdefault("order", {})
    current = order.get(real_provider, [])

    if present:
        # Put our profile first, keep any others after (preserve user intent).
        new_order = [pid] + [p for p in current if p != pid]
        if new_order == current:
            return  # no-op
        order[real_provider] = new_order
    else:
        if pid not in current:
            return  # no-op
        new_order = [p for p in current if p != pid]
        if new_order:
            order[real_provider] = new_order
        else:
            order.pop(real_provider, None)

    _write_json(path, state)


# ---------------------------------------------------------------------------
# Global openclaw.json mutations
# ---------------------------------------------------------------------------

def _register_profile_in_global_config(provider: str) -> None:
    """Ensure ``auth.profiles.<provider>:agent`` exists in openclaw.json.

    The gateway only loads profiles it knows about from the registry — without
    this entry our per-agent secret file would be ignored.
    """
    real_provider = _real_provider(provider)
    pid = profile_id_for(real_provider)
    _, mode = _credential_for(provider, "")

    cfg = _read_json(OCPLATFORM_CONFIG, {})
    auth = cfg.setdefault("auth", {})
    profiles = auth.setdefault("profiles", {})
    desired = {"provider": real_provider, "mode": mode}
    if profiles.get(pid) == desired:
        return  # already registered, skip the rewrite (and the gateway reload churn)
    profiles[pid] = desired
    _write_json(OCPLATFORM_CONFIG, cfg)


# ---------------------------------------------------------------------------
# Resolution: SQLite → effective key
# ---------------------------------------------------------------------------

def _effective_secret(
    db: Session, agent: Agent, provider: str
) -> str | None:
    """Resolve the effective key for ``(agent, provider)``.

    1. Per-agent override (``AgentApiKey``)
    2. Owner's user-level key (``UserApiKey``)
    3. None
    """
    override = (
        db.query(AgentApiKey)
        .filter(AgentApiKey.agent_id == agent.id, AgentApiKey.provider == provider)
        .one_or_none()
    )
    if override:
        return override.api_key

    user_key = (
        db.query(UserApiKey)
        .filter(UserApiKey.user_id == agent.owner_user_id, UserApiKey.provider == provider)
        .one_or_none()
    )
    if user_key:
        return user_key.api_key

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_agent_provider(db: Session, agent: Agent, provider: str) -> None:
    """Reconcile one ``(agent, provider)`` pair with on-disk OpenClaw state.

    Writes (or removes) the per-agent profile + order + global registry entry
    so the gateway will pick up the effective key on its next request.
    """
    if not agent.harness_agent_id:
        log.debug("Agent id=%s has no harness_agent_id; skipping sync", agent.id)
        return

    secret = _effective_secret(db, agent, provider)
    try:
        if secret:
            _register_profile_in_global_config(provider)
            _write_per_agent_profile(agent.harness_agent_id, provider, secret)
            _set_per_agent_order(agent.harness_agent_id, provider, present=True)
            log.info(
                "Synced %s key for agent %s (effective from %s)",
                provider, agent.harness_agent_id,
                "override" if _has_override(db, agent.id, provider) else "user",
            )
        else:
            _remove_per_agent_profile(agent.harness_agent_id, provider)
            _set_per_agent_order(agent.harness_agent_id, provider, present=False)
            log.info("Cleared %s key for agent %s", provider, agent.harness_agent_id)
    except Exception:
        # Don't break the API call if disk write fails — log loud, move on.
        log.exception(
            "Failed to sync %s for agent %s", provider, agent.harness_agent_id,
        )


def sync_agent_all_providers(db: Session, agent: Agent) -> None:
    """Reconcile every provider that has either an override or owner-saved key.

    Used at agent creation and during full backfills.
    """
    providers: set[str] = set()
    providers.update(
        p for (p,) in db.query(AgentApiKey.provider)
        .filter(AgentApiKey.agent_id == agent.id).all()
    )
    providers.update(
        p for (p,) in db.query(UserApiKey.provider)
        .filter(UserApiKey.user_id == agent.owner_user_id).all()
    )
    for provider in providers:
        sync_agent_provider(db, agent, provider)


def sync_user_provider(db: Session, user_id: int, provider: str) -> None:
    """Re-sync this provider across **every agent owned by this user**.

    Agents that have a per-agent override are still synced — the override
    just shadows the user key in ``_effective_secret``, so the on-disk state
    stays correct either way (cheap, idempotent).
    """
    agents = db.query(Agent).filter(Agent.owner_user_id == user_id).all()
    for agent in agents:
        sync_agent_provider(db, agent, provider)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_override(db: Session, agent_id: int, provider: str) -> bool:
    return (
        db.query(AgentApiKey.id)
        .filter(AgentApiKey.agent_id == agent_id, AgentApiKey.provider == provider)
        .first()
        is not None
    )


def all_agents(db: Session) -> Iterable[Agent]:
    """Helper for backfill scripts."""
    return db.query(Agent).all()


# ---------------------------------------------------------------------------
# Custom providers (user-owned BYO LLM endpoints)
# ---------------------------------------------------------------------------
#
# Each ``CustomProvider`` row maps to a single entry under
# ``models.providers.<namespaced_id>`` in ``openclaw.json``. We namespace
# by user id so two users can register a provider with the same slug
# without clobbering each other:
#
#     u3-nucbox-llama, u7-nucbox-llama, ...
#
# Model defs go into the same block's ``models`` array; aliases get added
# to ``agents.defaults.models`` so they show up in the picker like any
# built-in model.


def namespaced_provider_id(user_id: int, slug: str) -> str:
    """Stable, collision-free provider id for ``openclaw.json``."""
    return f"u{user_id}-{slug}"


def _models_array_from(provider: CustomProvider) -> list[dict]:
    try:
        models = json.loads(provider.models_json or "[]")
    except json.JSONDecodeError:
        log.warning("CustomProvider id=%s has invalid models_json; treating as empty",
                    provider.id)
        return []
    if not isinstance(models, list):
        return []
    # Strip null values — OpenClaw's config schema rejects them on
    # optional fields (e.g. ``compat: null`` fails validation). Recurse
    # one level so nested optional objects are cleaned too.
    cleaned: list[dict] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        cleaned.append({k: v for k, v in m.items() if v is not None})
    return cleaned


def sync_custom_provider(provider: CustomProvider) -> None:
    """Write/refresh this provider's block in ``openclaw.json``.

    Idempotent: re-running produces the same on-disk state. Safe to call
    after every CRUD mutation. Best-effort — logs and continues on disk
    failures so the API call still succeeds.
    """
    try:
        cfg = _read_json(OCPLATFORM_CONFIG, {})
        models_root = cfg.setdefault("models", {})
        providers = models_root.setdefault("providers", {})

        pid = namespaced_provider_id(provider.user_id, provider.slug)
        block: dict = {
            "baseUrl": provider.base_url,
            "api": provider.api_type,
            "models": _models_array_from(provider),
        }
        if provider.api_key:
            # Inline the key. Same trust model as the rest of
            # ``openclaw.json`` — plaintext is the house style here.
            block["apiKey"] = provider.api_key
        providers[pid] = block

        # Also register each model as a default alias so it shows up in the
        # picker / list_models() output with a friendly name.
        defaults = (
            cfg.setdefault("agents", {})
               .setdefault("defaults", {})
               .setdefault("models", {})
        )
        # First, drop any stale aliases for this provider (model removed/renamed).
        prefix = f"{pid}/"
        for stale in [k for k in defaults if k.startswith(prefix)]:
            defaults.pop(stale, None)
        for m in block["models"]:
            mid = m.get("id")
            if not mid:
                continue
            defaults[f"{pid}/{mid}"] = {
                "alias": m.get("name") or mid,
            }

        _write_json(OCPLATFORM_CONFIG, cfg)
        log.info("Synced custom provider %s (%d models)",
                 pid, len(block["models"]))
        # Bounce the gateway so the running agent processes pick up the
        # new model defs (contextWindow, maxTokens, baseUrl, key). Debounced
        # — batched saves coalesce into a single restart.
        _schedule_gateway_restart_if_available()
    except Exception:
        log.exception("Failed to sync custom provider id=%s", provider.id)


def remove_custom_provider(user_id: int, slug: str) -> None:
    """Drop this provider's block + its model aliases from ``openclaw.json``.

    Safe to call when no on-disk entry exists.
    """
    pid = namespaced_provider_id(user_id, slug)
    try:
        cfg = _read_json(OCPLATFORM_CONFIG, {})
        providers = cfg.get("models", {}).get("providers", {}) or {}
        removed = providers.pop(pid, None) is not None

        defaults = (
            cfg.get("agents", {}).get("defaults", {}).get("models", {}) or {}
        )
        prefix = f"{pid}/"
        stale = [k for k in defaults if k.startswith(prefix)]
        for k in stale:
            defaults.pop(k, None)

        if removed or stale:
            _write_json(OCPLATFORM_CONFIG, cfg)
            log.info("Removed custom provider %s from openclaw.json", pid)
            _schedule_gateway_restart_if_available()
    except Exception:
        log.exception("Failed to remove custom provider %s", pid)


def _schedule_gateway_restart_if_available() -> None:
    """Bounce the gateway, swallowing import-time failures so tests/CI
    that don't have the systemd helper installed still work."""
    try:
        from .gateway_restart import schedule_gateway_restart
        schedule_gateway_restart()
    except Exception:
        log.exception("Could not schedule gateway restart")
