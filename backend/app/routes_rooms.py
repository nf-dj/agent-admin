"""List Matrix rooms a bot is currently joined to.

Owner-only. Useful for auditing **who's actually talking to this bot** \u2014
the dashboard only shows the owner's own DM, but the bot may be in many
other rooms (other users, ad-hoc groups, etc.).

Implementation: log in as the bot using its stored ``matrix_access_token``
and call:

  * ``GET /_matrix/client/v3/joined_rooms`` \u2192 list of joined room ids
  * ``GET /_matrix/client/v3/rooms/{id}/state/m.room.name`` \u2192 display name
  * ``GET /_matrix/client/v3/rooms/{id}/joined_members`` \u2192 member count + ids

We deliberately call as the bot rather than via Synapse's admin API \u2014
the bot's own view is what matters here, and it avoids requiring admin
credentials for a read that's logically the bot's own data.

Rooms with exactly two members are flagged as DMs and we identify the
"other" party (the human, not the bot itself).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import Agent, User, get_db
from .matrix_admin import matrix_admin, MatrixError
from .permissions import require_owner
from .schemas import AgentRoomOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-rooms"])


@router.get("/{agent_id}/rooms", response_model=list[AgentRoomOut])
def list_agent_rooms(agent_id: int,
                     current: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> list[AgentRoomOut]:
    """Return every Matrix room the bot's account is currently joined to.

    Returns 503 with a friendly message if Matrix isn't configured. Returns
    an empty list (200) if the bot has no Matrix account provisioned yet.
    """
    agent = require_owner(db, current, agent_id)

    if not matrix_admin.enabled:
        raise HTTPException(status_code=503, detail="Matrix not configured on this server.")

    if not (agent.matrix_user_id and agent.matrix_access_token):
        # Bot was created before Matrix integration, or provisioning failed.
        # An empty list is a valid \u2014 if uninformative \u2014 answer.
        return []

    token = agent.matrix_access_token
    bot_user_id = agent.matrix_user_id

    try:
        joined = matrix_admin._request(
            "GET", "/_matrix/client/v3/joined_rooms", token=token, timeout=10,
        )
    except MatrixError as e:
        log.warning("joined_rooms failed for bot %s: %s", bot_user_id, e)
        # Stale/revoked token: surface as 502, the UI shows a re-provision hint.
        raise HTTPException(status_code=502, detail=f"Matrix call failed: {e}") from e

    room_ids: list[str] = joined.get("joined_rooms", [])
    out: list[AgentRoomOut] = []

    # We fetch each room's name and member list serially. Synapse handles
    # these fast (each is a small state query). If bots ever join hundreds
    # of rooms we can parallelise \u2014 not worth the complexity today.
    for rid in room_ids:
        name = _room_display_name(token, rid)
        members = _joined_member_ids(token, rid)
        # Exclude the bot itself when identifying "the other party" for DMs.
        others = [m for m in members if m != bot_user_id]
        is_dm = len(members) == 2
        out.append(AgentRoomOut(
            room_id=rid,
            name=name or _fallback_name(others, rid),
            member_count=len(members),
            is_dm=is_dm,
            other_user_id=others[0] if is_dm and others else None,
        ))

    # Sort: DMs first (typically the most actionable), then by name.
    out.sort(key=lambda r: (not r.is_dm, (r.name or "").lower()))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _room_display_name(token: str, room_id: str) -> str | None:
    """Best-effort fetch of ``m.room.name``. Returns None for unnamed rooms.

    Matrix returns 404 for rooms that simply have no name event set \u2014 that's
    fine, we'll fall back to a member-based label.
    """
    try:
        state = matrix_admin._request(
            "GET",
            f"/_matrix/client/v3/rooms/{_quote(room_id)}/state/m.room.name",
            token=token, timeout=10,
        )
        name = state.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    except MatrixError as e:
        if e.status == 404:
            return None
        log.debug("room name fetch failed for %s: %s", room_id, e)
        return None


def _joined_member_ids(token: str, room_id: str) -> list[str]:
    """Return the matrix user-ids of every joined member."""
    try:
        resp = matrix_admin._request(
            "GET",
            f"/_matrix/client/v3/rooms/{_quote(room_id)}/joined_members",
            token=token, timeout=10,
        )
    except MatrixError as e:
        log.debug("joined_members fetch failed for %s: %s", room_id, e)
        return []
    joined: dict[str, Any] = resp.get("joined", {})
    return list(joined.keys())


def _fallback_name(others: list[str], room_id: str) -> str:
    """Display label when a room has no ``m.room.name`` set.

    For DMs that's the common case (Matrix DMs are typically unnamed) \u2014
    fall back to the other party's user id. For unnamed group rooms we
    show the room id so the owner can at least identify it.
    """
    if len(others) == 1:
        return others[0]
    if others:
        return f"{others[0]} +{len(others) - 1}"
    return room_id


def _quote(s: str) -> str:
    """URL-quote a room/user id for use in a Matrix REST path."""
    from urllib.parse import quote
    return quote(s, safe="")
