"""Permission helpers for agent access control.

Roles:
    owner  -- created the bot (or was promoted). Full control: edit settings,
              invite/remove members, delete, view secrets.
    member -- invited by an owner. Can chat with the bot via the web UI.
              CANNOT read settings, secrets, invite others, or modify.

Use these helpers as FastAPI dependencies on any route that touches an
agent so the permission check is centralised and uniform.
"""
from __future__ import annotations
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from .db import Agent, AgentMember, User


ROLE_RANK = {"member": 1, "owner": 2}


def get_membership(db: Session, agent_id: int, user_id: int) -> AgentMember | None:
    return db.scalar(
        select(AgentMember).where(
            AgentMember.agent_id == agent_id,
            AgentMember.user_id == user_id,
        )
    )


def get_agent_with_role(db: Session, user: User, agent_id: int,
                        *, min_role: str = "member") -> tuple[Agent, str]:
    """Look up an agent and the current user's role on it.

    Raises 404 if the agent doesn't exist or the user has no role on it.
    Raises 403 if the user's role is below ``min_role``.

    Returns (agent, user_role).
    """
    a = db.get(Agent, agent_id)
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    m = get_membership(db, agent_id, user.id)
    if m is None:
        # Defense-in-depth: also accept the legacy owner_user_id field in case
        # the backfill migration hasn't run yet (shouldn't happen post-init_db).
        if a.owner_user_id == user.id:
            return a, "owner"
        # Don't leak existence to non-members.
        raise HTTPException(status_code=404, detail="agent not found")
    if ROLE_RANK.get(m.role, 0) < ROLE_RANK.get(min_role, 999):
        raise HTTPException(status_code=403, detail=f"requires role={min_role}")
    return a, m.role


def require_owner(db: Session, user: User, agent_id: int) -> Agent:
    a, _ = get_agent_with_role(db, user, agent_id, min_role="owner")
    return a
