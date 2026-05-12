"""Apply WhatsApp routing rules to portal rooms.

This module reconciles desired-state (the ``wa_routing_rules`` table +
``agents.whatsapp_login_id`` subscribers) with actual-state (Matrix room
memberships in the mautrix-whatsapp portals).

For each portal on a managed number we compute the **routed bot** for
that contact and make sure exactly that bot's MXID is joined to the
room. All other subscribed bots get kicked from the room (so they don't
receive every WA message and try to reply in lockstep).

Bridge relay mode (``!wa set-relay``) is also (re-)applied so the routed
bot's outbound Matrix messages get forwarded to WhatsApp via the paired
user's WA login.

This is best-effort and idempotent. Errors per-portal are accumulated
into the returned ``ApplyReport`` for the UI to surface.
"""
from __future__ import annotations
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from .db import Agent, WhatsAppRoutingRule, WebMatrixCredential, User
from .matrix_admin import matrix_admin, MatrixError
from .whatsapp_bridge_db import bridge_db, BridgePortal


log = logging.getLogger(__name__)


@dataclass
class PortalApplyResult:
    portal_mxid: str
    contact_jid: str
    routed_agent_id: int | None
    routed_agent_name: str | None
    invited: list[str] = field(default_factory=list)
    kicked: list[str] = field(default_factory=list)
    relayed: bool = False
    skipped_reason: str | None = None
    error: str | None = None


@dataclass
class ApplyReport:
    wa_login_id: str
    portals: list[PortalApplyResult] = field(default_factory=list)

    @property
    def total_portals(self) -> int:
        return len(self.portals)

    @property
    def changed_portals(self) -> int:
        return sum(
            1 for p in self.portals
            if (p.invited or p.kicked or p.relayed) and not p.error
        )

    @property
    def errored_portals(self) -> int:
        return sum(1 for p in self.portals if p.error)


def _resolve_routed_agent_id(
    contact_jid: str,
    rules: list[WhatsAppRoutingRule],
    subscribers: list[Agent],
) -> Optional[int]:
    """Pick which agent should be in this portal.

    Priority:
      1. A per-contact rule (``contact_jid == this``), lowest priority wins.
      2. The ``'*'`` fallback rule, if any.
      3. If exactly one subscriber, that one.
      4. None — ambiguous, leave the portal alone.
    """
    matching = [r for r in rules if r.contact_jid == contact_jid]
    if matching:
        matching.sort(key=lambda r: (r.priority, r.id))
        return matching[0].agent_id
    star = [r for r in rules if r.contact_jid == "*"]
    if star:
        star.sort(key=lambda r: (r.priority, r.id))
        return star[0].agent_id
    if len(subscribers) == 1:
        return subscribers[0].id
    return None


def _send_bridge_command(room_id: str, sender_token: str, command: str) -> None:
    """Fire-and-forget bridge command send.

    Duplicated from routes_agent_whatsapp to avoid a circular import.
    """
    encoded = urllib.parse.quote(room_id, safe="")
    txn_id = f"agent-admin-cmd-{int(time.time() * 1000)}"
    matrix_admin._request(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded}/send/m.room.message/{txn_id}",
        body={"msgtype": "m.text", "body": command},
        token=sender_token,
    )


def _ensure_owner_joined(room_id: str, owner_token: str) -> None:
    """Idempotent join of the owner into the portal room."""
    encoded = urllib.parse.quote(room_id, safe="")
    matrix_admin._request(
        "POST",
        f"/_matrix/client/v3/rooms/{encoded}/join",
        body={},
        token=owner_token,
    )


def apply_routing_for_number(
    db: Session,
    user: User,
    wa_login_id: str,
) -> ApplyReport:
    """Reconcile portal memberships for all DM portals on ``wa_login_id``.

    Does NOT commit anything to the DB; only reads rules + writes Matrix
    state. Safe to call repeatedly.
    """
    report = ApplyReport(wa_login_id=wa_login_id)

    cred = (
        db.query(WebMatrixCredential)
        .filter(WebMatrixCredential.user_id == user.id)
        .one_or_none()
    )
    if cred is None:
        log.warning("apply_routing: user %s has no Matrix credentials", user.id)
        return report

    subscribers: list[Agent] = (
        db.query(Agent)
        .filter(Agent.owner_user_id == user.id,
                Agent.whatsapp_login_id == wa_login_id)
        .order_by(Agent.id)
        .all()
    )
    if not subscribers:
        log.info("apply_routing: no subscribers on %s for user %s",
                 wa_login_id, user.id)
        return report

    rules: list[WhatsAppRoutingRule] = (
        db.query(WhatsAppRoutingRule)
        .filter(WhatsAppRoutingRule.user_id == user.id,
                WhatsAppRoutingRule.wa_login_id == wa_login_id)
        .all()
    )

    if not bridge_db.configured:
        log.warning("apply_routing: bridge DB not configured; nothing to do")
        return report

    portals: list[BridgePortal] = bridge_db.list_portals_for_login(
        cred.matrix_user_id, wa_login_id, room_types=["dm"],
    )

    # Subscriber MXID maps — used to identify "our bots" in the room
    # member list. We never touch users we don't manage.
    subscriber_mxids: dict[int, str] = {
        a.id: a.matrix_user_id for a in subscribers if a.matrix_user_id
    }
    subscriber_ids_by_mxid: dict[str, int] = {
        v: k for k, v in subscriber_mxids.items()
    }
    subscriber_name_by_id: dict[int, str] = {
        a.id: a.display_name for a in subscribers
    }
    # Each subscriber's own access token — used for self-leave. Mautrix
    # portal rooms set users_default=0 and kick=50, so the owner cannot
    # kick a bot directly; the bot has to leave itself.
    subscriber_tokens_by_mxid: dict[str, str] = {
        a.matrix_user_id: a.matrix_access_token
        for a in subscribers
        if a.matrix_user_id and a.matrix_access_token
    }

    for p in portals:
        if not p.mxid:
            continue
        result = PortalApplyResult(
            portal_mxid=p.mxid,
            contact_jid=p.portal_id,
            routed_agent_id=None,
            routed_agent_name=None,
        )

        routed = _resolve_routed_agent_id(p.portal_id, rules, subscribers)
        if routed is None:
            result.skipped_reason = (
                "ambiguous: multiple subscribers and no matching or '*' rule"
            )
            report.portals.append(result)
            continue

        routed_mxid = subscriber_mxids.get(routed)
        if not routed_mxid:
            result.skipped_reason = (
                f"routed agent {routed} has no Matrix user id"
            )
            report.portals.append(result)
            continue

        result.routed_agent_id = routed
        result.routed_agent_name = subscriber_name_by_id.get(routed)

        # Owner must be in the room to invite/kick.
        try:
            _ensure_owner_joined(p.mxid, cred.access_token)
        except MatrixError as e:
            result.error = f"owner couldn't join portal: {e}"
            log.warning("apply_routing: %s", result.error)
            report.portals.append(result)
            continue

        try:
            current_members = set(matrix_admin.get_room_member_ids(
                p.mxid, token=cred.access_token,
                memberships=("join", "invite"),
            ))
        except MatrixError as e:
            result.error = f"couldn't read members: {e}"
            log.warning("apply_routing: %s", result.error)
            report.portals.append(result)
            continue

        # 1. Invite the routed bot if it's not already there.
        if routed_mxid not in current_members:
            try:
                matrix_admin.invite_user_to_room(
                    p.mxid, routed_mxid,
                    inviter_token=cred.access_token,
                )
                result.invited.append(routed_mxid)
            except MatrixError as e:
                result.error = f"invite {routed_mxid} failed: {e}"
                log.warning("apply_routing: %s", result.error)

        # 2. Have any OTHER subscriber bots leave the room.
        #
        # Mautrix portals set users_default=0 and kick=50, so @web_u1
        # cannot kick bots even though they're our bots. Each bot has
        # its own access token though, so we just have them self-leave.
        # As a fallback (no bot token stored, or self-leave failed) we
        # try an owner-kick anyway — mostly so the error message in the
        # apply report tells the user why.
        for mxid in current_members:
            if mxid not in subscriber_ids_by_mxid:
                continue  # not one of our bots — hands off
            if mxid == routed_mxid:
                continue

            bot_token = subscriber_tokens_by_mxid.get(mxid)
            left = False
            if bot_token:
                try:
                    matrix_admin.leave_room(p.mxid, token=bot_token)
                    left = True
                except MatrixError as e:
                    log.warning(
                        "apply_routing: self-leave %s from %s failed: %s",
                        mxid, p.mxid, e,
                    )
            if left:
                result.kicked.append(mxid)
                continue

            # Fallback path — try the owner-kick. Almost always 403 in
            # mautrix portals (power_level mismatch), but surfacing the
            # error helps users understand why a stale bot is stuck.
            try:
                matrix_admin.kick_user_from_room(
                    p.mxid, mxid,
                    kicker_token=cred.access_token,
                    reason="routed to a different bot per agent-admin rules",
                )
                result.kicked.append(mxid)
            except MatrixError as e:
                msg = f"couldn't remove {mxid} (no bot token, owner-kick failed): {e}"
                result.error = msg if not result.error else f"{result.error}; {msg}"
                log.warning("apply_routing: %s", msg)

        # 3. Re-apply set-relay. Idempotent on the bridge side.
        try:
            _send_bridge_command(p.mxid, cred.access_token, "!wa set-relay")
            result.relayed = True
        except MatrixError as e:
            msg = f"!wa set-relay failed: {e}"
            result.error = msg if not result.error else f"{result.error}; {msg}"
            log.warning("apply_routing: %s", msg)

        report.portals.append(result)

    # Best-effort: nudge each newly-invited bot to accept its invites.
    try:
        from .sync import _accept_matrix_invites
        seen: set[int] = set()
        for r in report.portals:
            if r.routed_agent_id and r.routed_agent_id not in seen:
                seen.add(r.routed_agent_id)
                agent = db.query(Agent).filter(
                    Agent.id == r.routed_agent_id,
                ).one_or_none()
                if agent is not None:
                    try:
                        _accept_matrix_invites(agent)
                    except Exception:
                        log.exception(
                            "accept-invites loop failed for agent %s",
                            r.routed_agent_id,
                        )
    except Exception:
        log.exception("post-apply accept loop crashed")

    return report
