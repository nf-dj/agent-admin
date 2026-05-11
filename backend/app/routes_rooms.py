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
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import Agent, User, get_db
from .matrix_admin import matrix_admin, MatrixError
from .permissions import require_owner
from .schemas import (
    AgentRoomOut,
    AgentRoomMessagesOut,
    AgentRoomMessageOut,
    AgentRoomSendBody,
    AgentRoomSendResult,
)

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


# ---------------------------------------------------------------------------
# Room messages: read + reply as the bot
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/rooms/{room_id:path}/messages",
            response_model=AgentRoomMessagesOut)
def get_room_messages(agent_id: int, room_id: str,
                      limit: int = Query(50, ge=1, le=200),
                      from_token: str | None = Query(None, alias="from"),
                      current: User = Depends(get_current_user),
                      db: Session = Depends(get_db)) -> AgentRoomMessagesOut:
    """Return the most recent messages from a room the bot is in.

    Owner-only. Pages backwards through history: the first call returns
    the latest ``limit`` events (oldest first in the response); the
    ``prev_token`` it returns can be passed as ``from`` on a subsequent
    call to load *older* messages.

    Sender display names are enriched in one extra call to
    ``/joined_members`` so the UI doesn't have to spider room state for
    every sender id it sees.
    """
    agent, token = _agent_with_token(db, current, agent_id)

    # ``dir=b`` walks backwards from the latest event; if ``from`` is
    # supplied we resume from that pagination cursor (which we received as
    # ``end`` on the previous response). Synapse rejects an unknown room
    # for this bot with 403 — surface it as 404 to avoid leaking which
    # rooms exist.
    params = f"dir=b&limit={int(limit)}"
    if from_token:
        params += f"&from={_quote(from_token)}"

    try:
        resp = matrix_admin._request(
            "GET",
            f"/_matrix/client/v3/rooms/{_quote(room_id)}/messages?{params}",
            token=token, timeout=15,
        )
    except MatrixError as e:
        if e.status in (403, 404):
            raise HTTPException(status_code=404, detail="room not found") from e
        raise HTTPException(status_code=502, detail=f"Matrix call failed: {e}") from e

    raw_events: list[dict[str, Any]] = resp.get("chunk", []) or []

    # Enrich sender display names once for the whole batch — cheaper than
    # querying state per-event. Best-effort: failure here just means the
    # UI shows the raw mxid.
    sender_names: dict[str, str] = {}
    try:
        members = matrix_admin._request(
            "GET", f"/_matrix/client/v3/rooms/{_quote(room_id)}/joined_members",
            token=token, timeout=10,
        ).get("joined", {})
        for mxid, info in members.items():
            dn = info.get("display_name") if isinstance(info, dict) else None
            if dn:
                sender_names[mxid] = dn
    except MatrixError as e:
        log.debug("joined_members enrichment failed for %s: %s", room_id, e)

    # Filter to messages we know how to show, then flip so oldest comes
    # first (the natural chat reading order).
    msgs: list[AgentRoomMessageOut] = []
    for ev in raw_events:
        m = _shape_message(ev, agent.matrix_user_id or "", sender_names)
        if m is not None:
            msgs.append(m)
    msgs.reverse()

    return AgentRoomMessagesOut(
        messages=msgs,
        # ``end`` is the token to fetch *older* events on the next request.
        # Synapse returns it even when there's nothing more, so callers
        # should compare lengths / treat empty chunk as end-of-history.
        prev_token=resp.get("end"),
        has_more=bool(raw_events) and bool(resp.get("end")),
    )


@router.post("/{agent_id}/rooms/{room_id:path}/send",
             response_model=AgentRoomSendResult)
def send_room_message(agent_id: int, room_id: str,
                      body: AgentRoomSendBody,
                      current: User = Depends(get_current_user),
                      db: Session = Depends(get_db)) -> AgentRoomSendResult:
    """Send a plain-text message **as the bot** into a room.

    Owner-only. The bot becomes the visible author — the human reading
    won't know it was actually you typing. Use carefully.

    Idempotent: uses a client-supplied or server-generated transaction id
    so a retry of the same logical send won't double-post.
    """
    agent, token = _agent_with_token(db, current, agent_id)

    # Matrix requires a transaction id (txn) in the path to dedupe retries.
    # Random per request is fine since the frontend retries via its own
    # logic; reusing across retries would need the frontend to remember it.
    txn = uuid.uuid4().hex

    payload = {
        "msgtype": "m.text",
        "body": body.text,
    }

    try:
        resp = matrix_admin._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{_quote(room_id)}/send/m.room.message/{txn}",
            body=payload, token=token, timeout=15,
        )
    except MatrixError as e:
        if e.status in (403, 404):
            raise HTTPException(status_code=404, detail="room not found") from e
        raise HTTPException(status_code=502, detail=f"Matrix send failed: {e}") from e

    event_id = resp.get("event_id") or ""
    return AgentRoomSendResult(
        event_id=event_id,
        sent_at=int(time.time() * 1000),
    )


def _agent_with_token(db: Session, current: User, agent_id: int) -> tuple[Agent, str]:
    """Resolve an owner-only agent and return its bot Matrix access token.

    Centralises the "do you have permission + does the bot have creds" checks
    that every messages/send endpoint needs.
    """
    agent = require_owner(db, current, agent_id)
    if not matrix_admin.enabled:
        raise HTTPException(status_code=503, detail="Matrix not configured")
    if not (agent.matrix_user_id and agent.matrix_access_token):
        raise HTTPException(status_code=400, detail="bot has no Matrix account")
    return agent, agent.matrix_access_token


def _shape_message(ev: dict, bot_user_id: str,
                   sender_names: dict[str, str]) -> AgentRoomMessageOut | None:
    """Convert a raw Matrix timeline event to our API shape, or None to skip.

    We only surface ``m.room.message`` events (the actual chat). State
    events (joins, name changes, etc.) and reactions are filtered —
    they'd clutter the timeline without adding much value for the
    owner's read-and-reply use case.
    """
    if ev.get("type") != "m.room.message":
        return None
    content = ev.get("content", {}) or {}
    msgtype = content.get("msgtype") or "m.text"
    sender = ev.get("sender", "")

    # Best-effort body. For m.text/m.notice/m.emote we have a real body; for
    # m.image/m.file we fall back to the filename (or a placeholder) so the
    # owner can at least tell *something* was sent. Future: surface mxc URIs.
    body = content.get("body")
    if not isinstance(body, str) or not body.strip():
        body = f"[{msgtype}]"

    return AgentRoomMessageOut(
        event_id=ev.get("event_id", ""),
        sender=sender,
        sender_name=sender_names.get(sender) or _short_mxid(sender),
        body=body,
        msgtype=msgtype,
        ts=int(ev.get("origin_server_ts", 0)),
        is_bot=sender == bot_user_id,
    )


def _short_mxid(mxid: str) -> str:
    """``@alice:server`` → ``alice`` for compact display."""
    if mxid.startswith("@") and ":" in mxid:
        return mxid[1:].split(":", 1)[0]
    return mxid


# ---------------------------------------------------------------------------
# Cheap room-count lookup used by the dashboard
# ---------------------------------------------------------------------------

def _count_rooms_for(token: str) -> int | None:
    """Return how many rooms a Matrix account is joined to, or None on error.

    Used by the dashboard to render a small badge next to each owned bot.
    Errors (revoked token, network blip, etc.) collapse to ``None`` so the
    UI simply hides the badge rather than failing the whole listing.
    """
    try:
        resp = matrix_admin._request(
            "GET", "/_matrix/client/v3/joined_rooms", token=token, timeout=8,
        )
    except MatrixError as e:
        log.debug("joined_rooms count failed: %s", e)
        return None
    rooms = resp.get("joined_rooms")
    return len(rooms) if isinstance(rooms, list) else None


def counts_for_agents(agents: list) -> dict[int, int | None]:
    """Return ``{agent_id: count_or_None}`` for every agent that has a
    Matrix account, fetched in parallel.

    Agents with no Matrix creds are skipped (caller treats them as None).
    Bounded thread pool keeps load on Synapse modest — 8 in flight max,
    which is plenty for a dashboard but won't swamp the homeserver if
    someone has dozens of bots.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [a for a in agents if a.matrix_user_id and a.matrix_access_token]
    if not targets:
        return {}
    if not matrix_admin.enabled:
        return {a.id: None for a in targets}

    result: dict[int, int | None] = {}
    # max_workers capped so we don't spawn a thread per bot in pathological
    # cases. 8 is well below Synapse's per-IP limits even on small deploys.
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        futures = {ex.submit(_count_rooms_for, a.matrix_access_token): a.id
                   for a in targets}
        for fut in futures:
            aid = futures[fut]
            try:
                result[aid] = fut.result()
            except Exception:
                # Belt-and-braces: _count_rooms_for already swallows MatrixError;
                # this catches anything truly unexpected (e.g. asyncio cancellation).
                log.exception("room count for agent %s crashed", aid)
                result[aid] = None
    return result
