"""Agent membership routes — invite by email, list, remove.

Endpoints:
  GET    /api/agents/{agent_id}/members           (member-or-up)
  POST   /api/agents/{agent_id}/members           (owner)   {email}
  DELETE /api/agents/{agent_id}/members/{user_id} (owner OR self)

Only `owner` and `member` roles exist in v1. Owner CANNOT be removed via
this endpoint — to transfer or release ownership, delete the agent.
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from .auth import get_current_user
from .db import get_db, AgentMember, User
from .permissions import get_agent_with_role, require_owner, get_membership
from .schemas import AgentMemberOut, AgentMemberInvite


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agent-members"])


def _member_out(m: AgentMember, u: User) -> AgentMemberOut:
    return AgentMemberOut(
        user_id=u.id,
        email=u.email,
        display_name=u.display_name,
        role=m.role,
        created_at=m.created_at,
    )


@router.get("/{agent_id}/members", response_model=list[AgentMemberOut])
def list_members(agent_id: int,
                 current: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    # Anyone with any role on the agent can see the member list.
    # (Keeps things social — members know who else has access.)
    get_agent_with_role(db, current, agent_id, min_role="member")
    rows = db.execute(
        select(AgentMember, User)
        .join(User, User.id == AgentMember.user_id)
        .where(AgentMember.agent_id == agent_id)
        .order_by(AgentMember.created_at.asc())
    ).all()
    return [_member_out(m, u) for m, u in rows]


@router.post("/{agent_id}/members", response_model=AgentMemberOut, status_code=201)
def invite_member(agent_id: int, body: AgentMemberInvite,
                  current: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Owner-only: invite an existing user (by email) as a member.

    The invitee must already have an account. We don't email-send links —
    the owner is expected to tell them out-of-band that they have access.
    """
    require_owner(db, current, agent_id)

    target = db.scalar(select(User).where(User.email == body.email.lower()))
    if target is None:
        # Try case-insensitive fallback (User.email is stored as-provided).
        target = db.scalar(select(User).where(User.email.ilike(body.email)))
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"No user with email '{body.email}'. Ask them to sign up first.",
        )

    if target.id == current.id:
        raise HTTPException(status_code=400, detail="You're already the owner.")

    existing = get_membership(db, agent_id, target.id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{target.email} is already a {existing.role} of this agent.",
        )

    m = AgentMember(
        agent_id=agent_id,
        user_id=target.id,
        role="member",
        invited_by_user_id=current.id,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    log.info("agent %s: user %s invited %s as member", agent_id, current.id, target.id)
    return _member_out(m, target)


@router.delete("/{agent_id}/members/{user_id}")
def remove_member(agent_id: int, user_id: int,
                  current: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Remove a member. Owner can remove any member; a member can remove
    themselves (leave). Owners cannot be removed via this endpoint."""
    a, my_role = get_agent_with_role(db, current, agent_id, min_role="member")

    target = get_membership(db, agent_id, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="not a member of this agent")

    if target.role == "owner":
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the owner. Delete the agent instead.",
        )

    # Permission: owner can remove anyone; member can only remove self.
    if my_role != "owner" and user_id != current.id:
        raise HTTPException(status_code=403, detail="members can only remove themselves")

    db.delete(target)
    db.commit()
    log.info("agent %s: user %s removed member %s", agent_id, current.id, user_id)
    return {"ok": True}
