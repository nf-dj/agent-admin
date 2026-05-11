"""SQLite -> OpenClaw sync layer.

Routes manage agent rows in SQLite (the source of truth).
This module pushes those rows out to the underlying harness (OpenClaw CLI
+ openclaw.json) so the agents are actually runnable.

Design:
- Sync is the ONLY code that talks to the harness for writes.
- Each sync operation is idempotent: re-running it should converge state.
- Failures are logged but do not corrupt SQLite (DB is canonical).
- Future: this can be moved to a background worker / queue without changing
  the route code.
"""
from __future__ import annotations
import logging
from typing import Literal

from sqlalchemy.orm import Session

from .db import Agent
from .harness import get_harness
from .harness.base import AgentSpec, MatrixAccount
from .matrix_admin import matrix_admin, MatrixError
from .gateway_restart import schedule_gateway_restart


log = logging.getLogger(__name__)

SyncOp = Literal["create", "update", "delete"]


class SyncResult:
    def __init__(self, ok: bool, op: SyncOp, agent_id: str, error: str | None = None):
        self.ok = ok
        self.op = op
        self.agent_id = agent_id
        self.error = error

    def __repr__(self) -> str:
        return f"<SyncResult op={self.op} agent_id={self.agent_id} ok={self.ok} error={self.error!r}>"


def _spec_from_agent(a: Agent, *, include_telegram_token: bool = False,
                     include_matrix: bool = False) -> AgentSpec:
    """Build a harness spec from a DB row.

    `include_telegram_token` and `include_matrix` control whether we re-push
    each channel's credentials to the harness. Only do this when the
    credentials just changed; otherwise we leave existing registrations alone.
    """
    matrix_acct = None
    if include_matrix and a.matrix_access_token and a.matrix_user_id:
        matrix_acct = MatrixAccount(
            account_id=a.matrix_account_id or a.harness_agent_id,
            homeserver=matrix_admin.homeserver,
            user_id=a.matrix_user_id,
            access_token=a.matrix_access_token,
        )
    return AgentSpec(
        harness_agent_id=a.harness_agent_id,
        display_name=a.display_name,
        model=a.model,
        emoji=a.emoji,
        system_prompt=a.system_prompt,
        workspace_path=a.workspace_path,
        telegram_bot_token=a.telegram_bot_token if include_telegram_token else None,
        telegram_account_id=a.telegram_account_id,
        matrix_account=matrix_acct,
    )


def _ensure_matrix_user(db: Session, agent: Agent) -> None:
    """Provision a Matrix user for the agent if Matrix is enabled and none exists."""
    if not matrix_admin.enabled:
        return
    if agent.matrix_user_id and agent.matrix_access_token:
        return  # already provisioned
    try:
        mu = matrix_admin.create_user(
            localpart_hint=agent.harness_agent_id,
            display_name=agent.display_name,
        )
        agent.matrix_user_id = mu.user_id
        agent.matrix_access_token = mu.access_token
        agent.matrix_device_id = mu.device_id
        agent.matrix_password = mu.password
        agent.matrix_account_id = agent.harness_agent_id
        db.commit()
        db.refresh(agent)
        log.info("Matrix user provisioned for agent %s: %s", agent.harness_agent_id, mu.user_id)
    except MatrixError as e:
        log.error("Matrix user provisioning failed for %s: %s", agent.harness_agent_id, e)
        # Don't raise — agent still works without Matrix; user can retry sync.
    except Exception as e:
        log.exception("Unexpected error provisioning Matrix user for %s", agent.harness_agent_id)


def sync_create(db: Session, agent: Agent) -> SyncResult:
    """Provision a brand-new agent in the harness.

    Also auto-provisions a Matrix user if Matrix is enabled and the agent
    doesn't already have one.
    """
    try:
        _ensure_matrix_user(db, agent)
        h = get_harness(agent.harness)
        spec = _spec_from_agent(agent, include_telegram_token=True, include_matrix=True)
        h.create_agent(spec)
        # Channel config changed — bounce the gateway so the new accounts go live.
        schedule_gateway_restart()
        _accept_matrix_invites(agent)
        log.info("sync_create ok: %s", agent.harness_agent_id)
        return SyncResult(True, "create", agent.harness_agent_id)
    except Exception as e:
        log.exception("sync_create failed for %s", agent.harness_agent_id)
        return SyncResult(False, "create", agent.harness_agent_id, str(e))


def _accept_matrix_invites(agent: Agent) -> None:
    """Best-effort: auto-join any rooms this bot has been invited to and tag DMs."""
    if not (matrix_admin.enabled and agent.matrix_access_token and agent.matrix_user_id):
        return
    try:
        n = matrix_admin.accept_pending_invites(agent.matrix_access_token, agent.matrix_user_id)
        if n:
            log.info("Auto-joined %d Matrix room(s) for %s", n, agent.harness_agent_id)
    except Exception:
        log.exception("accept_pending_invites failed for %s", agent.harness_agent_id)


def sync_update(db: Session, agent: Agent, *,
                telegram_token_changed: bool = False,
                matrix_changed: bool = False) -> SyncResult:
    """Push DB row state to the harness.

    Pass `telegram_token_changed=True` when the user just supplied a new token
    so we re-register the Telegram account. Pass `matrix_changed=True` to
    re-push the Matrix credentials (rare — typically only on token rotation).
    """
    try:
        # If Matrix is enabled but the agent has no user yet, provision one.
        _ensure_matrix_user(db, agent)
        h = get_harness(agent.harness)
        spec = _spec_from_agent(
            agent,
            include_telegram_token=telegram_token_changed,
            include_matrix=matrix_changed or bool(agent.matrix_access_token and not _matrix_already_registered(agent)),
        )
        h.update_agent(spec)
        # If we touched a channel token, restart the gateway so it picks it up.
        if telegram_token_changed or matrix_changed:
            schedule_gateway_restart()
        # Also push display name to Matrix on update + pick up any new invites
        if agent.matrix_user_id and matrix_admin.enabled:
            try:
                matrix_admin.set_display_name(agent.matrix_user_id, agent.display_name)
            except Exception:
                log.warning("Could not update Matrix display name for %s", agent.matrix_user_id)
            _accept_matrix_invites(agent)
        log.info("sync_update ok: %s", agent.harness_agent_id)
        return SyncResult(True, "update", agent.harness_agent_id)
    except Exception as e:
        log.exception("sync_update failed for %s", agent.harness_agent_id)
        return SyncResult(False, "update", agent.harness_agent_id, str(e))


def _matrix_already_registered(agent: Agent) -> bool:
    """Cheap check: does the OpenClaw config already have this Matrix account?"""
    try:
        h = get_harness(agent.harness)
        cfg = h._read_config()  # type: ignore[attr-defined]
        accts = (cfg.get("channels", {}).get("matrix", {}) or {}).get("accounts", {}) or {}
        return (agent.matrix_account_id or agent.harness_agent_id) in accts
    except Exception:
        return False


def sync_delete(harness_name: str, harness_agent_id: str,
                matrix_user_id: str | None = None) -> SyncResult:
    """Remove an agent from the harness, and deactivate its Matrix user if any."""
    errors: list[str] = []
    try:
        h = get_harness(harness_name)
        h.delete_agent(harness_agent_id)
        log.info("sync_delete (harness) ok: %s", harness_agent_id)
    except Exception as e:
        log.exception("sync_delete (harness) failed for %s", harness_agent_id)
        errors.append(f"harness: {e}")

    if matrix_user_id and matrix_admin.enabled:
        try:
            matrix_admin.deactivate_user(matrix_user_id)
        except Exception as e:
            log.exception("sync_delete (matrix) failed for %s", matrix_user_id)
            errors.append(f"matrix: {e}")

    # Channel config changed; let the gateway pick up the removal too.
    schedule_gateway_restart()

    if errors:
        return SyncResult(False, "delete", harness_agent_id, "; ".join(errors))
    return SyncResult(True, "delete", harness_agent_id)


def sync_all(db: Session) -> list[SyncResult]:
    """Reconcile every DB agent into the harness.

    Useful for:
      - First-time bootstrap after restoring a DB.
      - Recovering from a harness-side wipe.
      - Periodic reconciliation jobs.

    Does NOT touch agents that exist in the harness but not in the DB
    (those are out-of-band, e.g. the `main` agent).
    """
    results: list[SyncResult] = []
    for a in db.query(Agent).all():
        try:
            h = get_harness(a.harness)
            state = h.get_agent_state(a.harness_agent_id)
            if state.exists:
                results.append(sync_update(db, a,
                                            telegram_token_changed=bool(a.telegram_bot_token),
                                            matrix_changed=bool(a.matrix_access_token)))
            else:
                results.append(sync_create(db, a))
        except Exception as e:
            log.exception("sync_all error for %s", a.harness_agent_id)
            results.append(SyncResult(False, "update", a.harness_agent_id, str(e)))
    return results
