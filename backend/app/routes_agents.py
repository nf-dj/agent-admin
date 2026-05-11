"""Agent CRUD routes — scoped to the current user's agents.

These routes only mutate SQLite (the source of truth). After each write
they delegate to `app.sync` to reconcile the change into the OpenClaw
harness. Sync failures are surfaced as warnings but do not roll back the
DB write — the DB is canonical and a later reconciliation can fix things.
"""
import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from .auth import get_current_user
from .db import get_db, Agent, AgentMember, User
from .schemas import (
    AgentCreate, AgentUpdate, AgentOut, AgentDetailOut, ModelOut, HarnessOut,
    AgentChatInfoOut,
)
from .harness import HARNESSES, get_harness
from .sync import sync_create, sync_update, sync_delete
from .config import settings
from .permissions import get_agent_with_role, require_owner


router = APIRouter(prefix="/api", tags=["agents"])


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "agent"


def _build_harness_agent_id(user: User, requested_slug: str | None,
                            display_name: str, db: Session) -> str:
    """Per-user namespaced agent id; ensures global uniqueness.

    Prefix order: ``user.company_prefix`` if set (e.g. ``acme-``),
    otherwise the legacy ``u<id>-`` fallback. Uniqueness across all users
    is still enforced via the loop below, so two companies that happen to
    collide get suffixed with ``-2``, ``-3``, etc.
    """
    base_slug = requested_slug or _slugify(display_name)
    custom = (getattr(user, "company_prefix", None) or "").strip()
    prefix = f"{custom}-" if custom else f"u{user.id}-"
    candidate = f"{prefix}{base_slug}"
    n = 1
    while True:
        existing_db = db.scalar(select(Agent.id).where(Agent.harness_agent_id == candidate))
        if existing_db is None:
            return candidate
        n += 1
        candidate = f"{prefix}{base_slug}-{n}"
        if n > 1000:
            raise HTTPException(status_code=500, detail="could not allocate agent id")


def _get_owned_agent(db: Session, user: User, agent_pk: int) -> Agent:
    """Legacy helper: owner-only access. Prefer the centralised
    ``permissions.require_owner`` / ``get_agent_with_role`` for new code.
    """
    return require_owner(db, user, agent_pk)


# ---------- metadata endpoints ----------
@router.get("/harnesses", response_model=list[HarnessOut])
def list_harnesses(current=Depends(get_current_user)):
    return [
        HarnessOut(name=name, display_name=name.capitalize(), available=True)
        for name in HARNESSES.keys()
    ]


@router.get("/models", response_model=list[ModelOut])
def list_models(harness: str = "openclaw", current=Depends(get_current_user)):
    try:
        h = get_harness(harness)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return [ModelOut(**m) for m in h.list_models()]


# ---------- agent CRUD ----------
@router.get("/agents", response_model=list[AgentOut])
def list_agents(current: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """All agents the current user can access: owned + shared with me.

    Each row carries the current user's role on that agent in ``my_role``.
    Owned agents additionally get a live ``room_count`` from Matrix — a
    cheap signal for "how many people are talking to my bot". Members'
    rooms are not exposed (they don't own the bot's audit surface).
    """
    rows = db.execute(
        select(Agent, AgentMember.role)
        .join(AgentMember, AgentMember.agent_id == Agent.id)
        .where(AgentMember.user_id == current.id)
        .order_by(Agent.created_at.desc())
    ).all()

    # Fetch room counts + skill counts only for owned agents — cheaper, and
    # prevents leaking aggregate audit data to non-owner members.
    from .routes_rooms import counts_for_agents
    from .routes_skills import count_skills_for
    owned_agents = [a for a, role in rows if role == "owner"]
    room_counts = counts_for_agents(owned_agents)
    skill_counts = {a.id: count_skills_for(a) for a in owned_agents}

    return [
        AgentOut.from_agent(a, my_role=role,
                            room_count=room_counts.get(a.id),
                            skill_count=skill_counts.get(a.id))
        for a, role in rows
    ]


@router.post("/agents", response_model=AgentDetailOut, status_code=201)
def create_agent(body: AgentCreate,
                 current: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    if body.harness not in HARNESSES:
        raise HTTPException(status_code=400, detail=f"unknown harness: {body.harness}")

    harness_agent_id = _build_harness_agent_id(current, body.slug, body.display_name, db)
    workspace_path = str(settings.oc_workspaces_root / harness_agent_id)

    # 1) Write to SQLite (source of truth).
    agent = Agent(
        owner_user_id=current.id,
        harness=body.harness,
        harness_agent_id=harness_agent_id,
        display_name=body.display_name,
        model=body.model,
        emoji=body.emoji,
        system_prompt=body.system_prompt,
        telegram_bot_token=body.telegram_bot_token,
        telegram_account_id=harness_agent_id if body.telegram_bot_token else None,
        workspace_path=workspace_path,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    # 1b) Seed the membership row for the creator (role=owner).
    db.add(AgentMember(
        agent_id=agent.id,
        user_id=current.id,
        role="owner",
        invited_by_user_id=None,
    ))
    db.commit()

    # 2) Sync to harness. If this fails, roll back DB so user can retry.
    result = sync_create(db, agent)
    if not result.ok:
        db.delete(agent)
        db.commit()
        raise HTTPException(status_code=500, detail=f"harness sync failed: {result.error}")

    # 3) Push the owner's saved API keys into the new agent's auth profile.
    # New agent won't have any AgentApiKey overrides yet, so this just copies
    # the user-level keys. Failures here are logged but don't fail creation.
    from .auth_sync import sync_agent_all_providers
    sync_agent_all_providers(db, agent)

    return AgentDetailOut.from_agent_with_runtime(agent, None, my_role="owner")


@router.get("/agents/{agent_id}", response_model=AgentDetailOut)
def get_agent(agent_id: int,
              current: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """Read full agent details. Owner-only (settings include secrets).

    Members should use /api/agents/{id}/chat-info instead.
    """
    a = require_owner(db, current, agent_id)
    return AgentDetailOut.from_agent_with_runtime(a, None, my_role="owner")


@router.get("/agents/{agent_id}/chat-info", response_model=AgentChatInfoOut)
def get_agent_chat_info(agent_id: int,
                        current: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Minimal agent info safe to expose to non-owner members.

    Used by ChatView to start a DM without revealing settings/secrets.
    """
    a, role = get_agent_with_role(db, current, agent_id, min_role="member")
    return AgentChatInfoOut(
        id=a.id,
        display_name=a.display_name,
        emoji=a.emoji,
        matrix_user_id=a.matrix_user_id,
        my_role=role,
    )


@router.get("/agents/{agent_id}/runtime")
def get_agent_runtime(agent_id: int,
                      current: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Live harness state for an agent (slower; not loaded by default)."""
    a = _get_owned_agent(db, current, agent_id)
    try:
        h = get_harness(a.harness)
        state = h.get_agent_state(a.harness_agent_id)
        return state.raw or {
            "exists": state.exists,
            "workspace_path": state.workspace_path,
            "model": state.model,
            "bindings": state.bindings,
        }
    except Exception as e:
        return {"error": str(e)}


@router.patch("/agents/{agent_id}", response_model=AgentDetailOut)
def update_agent(agent_id: int, body: AgentUpdate,
                 current: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    a = _get_owned_agent(db, current, agent_id)

    # 1) Mutate the SQLite row.
    telegram_token_changed = False
    if body.display_name is not None:
        a.display_name = body.display_name
    if body.model is not None:
        a.model = body.model
    if body.emoji is not None:
        a.emoji = body.emoji
    if body.system_prompt is not None:
        a.system_prompt = body.system_prompt
    if body.telegram_bot_token is not None and body.telegram_bot_token != "":
        a.telegram_bot_token = body.telegram_bot_token
        a.telegram_account_id = a.telegram_account_id or a.harness_agent_id
        telegram_token_changed = True
    db.commit()
    db.refresh(a)

    # 2) Push to harness. Failures here don't undo the DB write — they show
    #    up in logs and the user can retry / hit /sync later.
    sync_update(db, a, telegram_token_changed=telegram_token_changed)

    return AgentDetailOut.from_agent_with_runtime(a, None)


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: int,
                 current: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    a = _get_owned_agent(db, current, agent_id)
    harness_name = a.harness
    harness_agent_id = a.harness_agent_id
    matrix_user_id = a.matrix_user_id

    # 1) Remove from SQLite first.
    db.delete(a)
    db.commit()

    # 2) Sync the deletion to the harness (+ Matrix).
    result = sync_delete(harness_name, harness_agent_id, matrix_user_id=matrix_user_id)
    if not result.ok:
        return {"ok": True, "warning": f"removed from DB but sync failed: {result.error}"}
    return {"ok": True}


@router.post("/agents/{agent_id}/sync")
def resync_agent(agent_id: int,
                 current: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Manually reconcile a single agent's DB row into the harness.

    Useful when an earlier sync failed and the DB / harness drifted.
    """
    a = _get_owned_agent(db, current, agent_id)
    try:
        h = get_harness(a.harness)
        state = h.get_agent_state(a.harness_agent_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"harness probe failed: {e}")

    if state.exists:
        result = sync_update(db, a, telegram_token_changed=bool(a.telegram_bot_token))
    else:
        result = sync_create(db, a)

    if not result.ok:
        raise HTTPException(status_code=500, detail=result.error or "sync failed")
    return {"ok": True, "op": result.op}
