"""Per-agent WhatsApp binding.

Endpoints:
  GET /api/agents/{id}/whatsapp
    → Current binding for the bot + the owner's available WA logins.
  PUT /api/agents/{id}/whatsapp  body={ "login_id": "<id>"|null }
    → Bind/unbind a WA login_id to this bot. On binding, the bot's MXID
      is invited to every existing DM portal room for that login (the bot
      then auto-accepts via its standard invite loop).

Owner-only. The bot's MXID must already exist (Matrix integration enabled).

Future:
  - Background sync that auto-invites the bot to NEW portal rooms as they
    appear (currently only existing rooms at the time of binding).
  - Per-chat overrides (allow/deny specific chats).
  - Group-chat support (currently DMs only).
"""
from __future__ import annotations
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import SessionLocal, User, Agent, WebMatrixCredential
from .permissions import require_owner
from .matrix_admin import matrix_admin, MatrixError
from .whatsapp_bridge_db import bridge_db, BridgePortal
from .sync import _accept_matrix_invites
import json
import time
import urllib.parse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents", "whatsapp"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- schemas ----------------------------------------------------------------

class BoundPortal(BaseModel):
    """A WA portal room the bot is bound (or about to be bound) to."""
    portal_id: str
    mxid: str
    name: str
    room_type: str


class WhatsAppLoginOption(BaseModel):
    """One of the owner's linked WA accounts (for the dropdown)."""
    id: str
    name: str | None = None
    # How many DM portal rooms exist for this login (for the UI to show
    # "+66… (3 chats)").
    dm_count: int = 0
    # Set when this login is already pinned to a different bot in the same
    # account — the UI should mark it as "taken".
    taken_by_agent_id: int | None = None
    taken_by_agent_name: str | None = None


class AgentWhatsAppOut(BaseModel):
    agent_id: int
    whatsapp_login_id: str | None
    available_logins: list[WhatsAppLoginOption]
    # Portals the bot is currently bound to (i.e. live, joined).
    bound_portals: list[BoundPortal] = []


class AgentWhatsAppUpdate(BaseModel):
    login_id: str | None = None  # null = unbind


# --- helpers ----------------------------------------------------------------

async def _fetch_owner_logins(owner: User, db: Session) -> list[dict[str, Any]]:
    """Look up the owner's linked WA accounts via the provisioning API.

    Reuses ``routes_me._ensure_creds`` to get the owner's MXID.
    """
    from .routes_me import _ensure_creds
    from .main import app

    bridge = getattr(app.state, "whatsapp_bridge", None)
    if bridge is None:
        return []
    cred = _ensure_creds(db, owner)
    try:
        return await bridge.list_logins(cred.matrix_user_id)
    except Exception as e:
        log.warning("could not list WA logins for user %s: %s", owner.id, e)
        return []


def _portal_display_name(p: BridgePortal) -> str:
    if p.name:
        return p.name
    # Fall back to the WA JID with the @s.whatsapp.net suffix stripped.
    jid = p.portal_id
    if "@" in jid:
        jid = jid.split("@", 1)[0]
    return f"+{jid}" if jid.isdigit() else jid


def _list_dm_portals(owner_mxid: str, login_id: str) -> list[BridgePortal]:
    """Return DM portal rooms for this owner + login. Empty list on failure."""
    if not bridge_db.configured:
        return []
    return bridge_db.list_portals_for_login(owner_mxid, login_id, room_types=["dm"])


def _ensure_owner_joined(room_id: str, owner_token: str) -> None:
    """Make sure the owner has joined the portal room.

    Portal rooms are created by the bridge bot which invites the owner; the
    owner needs to actually accept before they can invite anyone else.
    Idempotent: POST /join on an already-joined room is a no-op success.
    """
    encoded = urllib.parse.quote(room_id, safe="")
    matrix_admin._request(
        "POST",
        f"/_matrix/client/v3/rooms/{encoded}/join",
        body={},
        token=owner_token,
    )


def _send_bridge_command(room_id: str, sender_token: str, command: str) -> None:
    """Send a bridge command (e.g. ``!wa set-relay``) as the given user.

    The bridge bot processes it server-side and posts a reply in the room.
    We don't wait for that reply — the command is fire-and-forget.
    """
    encoded = urllib.parse.quote(room_id, safe="")
    txn_id = f"agent-admin-cmd-{int(time.time() * 1000)}"
    matrix_admin._request(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded}/send/m.room.message/{txn_id}",
        body={"msgtype": "m.text", "body": command},
        token=sender_token,
    )


# --- endpoints --------------------------------------------------------------

@router.get("/{agent_id}/whatsapp", response_model=AgentWhatsAppOut)
async def get_agent_whatsapp(
    agent_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = require_owner(db, current, agent_id)

    logins = await _fetch_owner_logins(current, db)

    # Resolve owner's MXID once (might be needed for portal counts).
    cred = db.query(WebMatrixCredential).filter(
        WebMatrixCredential.user_id == current.id,
    ).one_or_none()
    owner_mxid = cred.matrix_user_id if cred else None

    # Map of login_id → agent that's already pinned to it (other than this one).
    # Enforces "one WA number per bot per user".
    pinned: dict[str, Agent] = {}
    pinned_rows = db.query(Agent).filter(
        Agent.owner_user_id == current.id,
        Agent.whatsapp_login_id.isnot(None),
        Agent.id != agent.id,
    ).all()
    for a in pinned_rows:
        pinned[a.whatsapp_login_id] = a

    options: list[WhatsAppLoginOption] = []
    for lg in logins:
        # The provisioning API returns either a list of strings (just login_ids,
        # current shape on v26.04) or a list of objects. Handle both.
        if isinstance(lg, str):
            lid, name = lg, None
        elif isinstance(lg, dict):
            lid = lg.get("id") or lg.get("login_id")
            name = (lg.get("remote_name")
                    or (lg.get("profile") or {}).get("name")
                    or (lg.get("profile") or {}).get("remote_name"))
        else:
            continue
        if not lid:
            continue
        dm_count = 0
        if owner_mxid:
            dm_count = len(_list_dm_portals(owner_mxid, lid))
        taken = pinned.get(lid)
        options.append(WhatsAppLoginOption(
            id=lid, name=name, dm_count=dm_count,
            taken_by_agent_id=taken.id if taken else None,
            taken_by_agent_name=taken.display_name if taken else None,
        ))

    bound_portals: list[BoundPortal] = []
    if agent.whatsapp_login_id and owner_mxid:
        for p in _list_dm_portals(owner_mxid, agent.whatsapp_login_id):
            bound_portals.append(BoundPortal(
                portal_id=p.portal_id, mxid=p.mxid or "",
                name=_portal_display_name(p), room_type=p.room_type,
            ))

    return AgentWhatsAppOut(
        agent_id=agent.id,
        whatsapp_login_id=agent.whatsapp_login_id,
        available_logins=options,
        bound_portals=bound_portals,
    )


@router.put("/{agent_id}/whatsapp", response_model=AgentWhatsAppOut)
async def set_agent_whatsapp(
    agent_id: int,
    body: AgentWhatsAppUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = require_owner(db, current, agent_id)
    new_login = (body.login_id or "").strip() or None

    if new_login is not None:
        # Validate: this user must own this login.
        owner_logins = await _fetch_owner_logins(current, db)
        owned_ids: set[str] = set()
        for lg in owner_logins:
            if isinstance(lg, str):
                owned_ids.add(lg)
            elif isinstance(lg, dict):
                v = lg.get("id") or lg.get("login_id")
                if v:
                    owned_ids.add(v)
        if new_login not in owned_ids:
            raise HTTPException(
                status_code=400,
                detail=f"You don't have a WhatsApp account with login_id {new_login!r}.",
            )

        # Block double-claims: another of this user's bots already owns it.
        other = db.query(Agent).filter(
            Agent.owner_user_id == current.id,
            Agent.whatsapp_login_id == new_login,
            Agent.id != agent.id,
        ).first()
        if other is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"WhatsApp number is already assigned to bot "
                        f"'{other.display_name}' (#{other.id}). "
                        f"Unassign it there first."),
            )

        # Bot must have a Matrix account to be invited to portal rooms.
        if not agent.matrix_user_id:
            raise HTTPException(
                status_code=400,
                detail="This bot doesn't have a Matrix account — can't bind WhatsApp.",
            )

    # Persist the binding first so a partial invite-failure doesn't leave
    # the user wondering what state we're in.
    agent.whatsapp_login_id = new_login
    db.add(agent)
    db.commit()
    db.refresh(agent)

    # Invite bot to existing portal rooms (best-effort; we don't roll back
    # the DB write if some invites fail — the UI surfaces per-room status).
    invited_count = 0
    relay_count = 0
    failed: list[str] = []
    if new_login is not None:
        cred = db.query(WebMatrixCredential).filter(
            WebMatrixCredential.user_id == current.id,
        ).one_or_none()
        if cred is None:
            raise HTTPException(
                status_code=500,
                detail="Owner has no Matrix credentials — can't issue invites.",
            )
        portals = _list_dm_portals(cred.matrix_user_id, new_login)
        for p in portals:
            if not p.mxid:
                continue
            # Step 1: ensure owner has accepted the portal invite.
            # The bridge creates the portal and invites @web_u1 but doesn't
            # force-join; @web_u1 needs to actually be IN the room to invite
            # anyone else.
            try:
                _ensure_owner_joined(p.mxid, cred.access_token)
            except MatrixError as e:
                log.warning("owner failed to join %s: %s", p.mxid, e)
                failed.append(p.mxid)
                continue
            # Step 2: owner invites the bot.
            try:
                matrix_admin.invite_user_to_room(
                    p.mxid, agent.matrix_user_id,
                    inviter_token=cred.access_token,
                )
                invited_count += 1
            except MatrixError as e:
                log.warning("invite to %s failed: %s", p.mxid, e)
                failed.append(p.mxid)
                continue
            # Step 3: enable relay mode so the bot's replies are bridged
            # to WhatsApp through the owner's WA session. Idempotent — the
            # bridge bot responds with the same "set as relay" message
            # even if it was already set, so we just send the command.
            try:
                _send_bridge_command(p.mxid, cred.access_token, "!wa set-relay")
                relay_count += 1
            except MatrixError as e:
                log.warning("set-relay in %s failed: %s", p.mxid, e)
                # Bot is in the room but can't reply via WA — surface as
                # partial failure.
                failed.append(p.mxid)

        # Nudge the bot to accept the invites it just got.
        try:
            _accept_matrix_invites(agent)
        except Exception:
            log.exception("post-invite accept loop failed for agent %s", agent.id)

    log.info(
        "agent %s whatsapp binding set to %r (invited %d portals, %d relayed, %d failed)",
        agent.id, new_login, invited_count, relay_count, len(failed),
    )

    # Return the fresh state (re-uses the GET assembly logic).
    return await get_agent_whatsapp(agent_id, current=current, db=db)
