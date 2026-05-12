"""WhatsApp per-contact routing rules (Phase 1: read-only).

A WA number can be shared across multiple bots: the existing
``agents.whatsapp_login_id`` column holds the *default* bot, and this
module manages contact-specific overrides via the ``wa_routing_rules``
table.

Endpoints (Phase 1):
  GET /api/whatsapp/numbers/{wa_login_id}/rules
    → All routing rules the current user has on this number, plus the
      number's "default bot" (the existing single bind), plus a snapshot
      of current portal rooms with the bot they're currently bound to.

  GET /api/whatsapp/numbers/{wa_login_id}/contacts
    → Known WA contacts on this number (from the bridge DB), suitable
      for populating a dropdown in the "Add rule" form.

Owner-only. ``wa_login_id`` is the bare numeric login (e.g. ``66939218159``),
not the full JID.

Phase 2 will add POST/DELETE for rules.
Phase 3 will add the worker that applies rules to portal rooms.
"""
from __future__ import annotations
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import SessionLocal, User, Agent, WhatsAppRoutingRule, WebMatrixCredential
from .whatsapp_bridge_db import bridge_db


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/whatsapp/numbers", tags=["whatsapp", "routing"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- schemas ----------------------------------------------------------------

class RoutingRuleOut(BaseModel):
    id: int
    wa_login_id: str
    contact_jid: str            # "*" = fallback rule
    contact_phone: str | None   # pretty form, e.g. "66909966651" (no JID suffix)
    agent_id: int
    agent_name: str
    priority: int


class DefaultBotOut(BaseModel):
    """The bot that ``agents.whatsapp_login_id`` currently points at.

    This is the implicit fallback: any contact without a matching rule
    gets routed here. Migrating it into ``wa_routing_rules`` as a
    ``contact_jid='*'`` row is a Phase-2 nice-to-have; for now we
    surface it separately so the UI can show both.
    """
    agent_id: int
    agent_name: str


class PortalSnapshot(BaseModel):
    """One existing portal room and the bot currently in it (if known).

    The bridge owns the room ↔ contact mapping. We can read the contact
    JID from the bridge DB but we can't (cheaply) ask Synapse "which of
    *our* bots is joined to this room" without an extra round-trip per
    room. For Phase 1 we just surface the contact + portal mxid and let
    Phase 3 enrich this with live membership state.
    """
    contact_jid: str
    contact_phone: str | None
    portal_mxid: str
    portal_name: str


class WhatsAppRoutingOut(BaseModel):
    wa_login_id: str
    default_bot: DefaultBotOut | None
    rules: list[RoutingRuleOut]
    portals: list[PortalSnapshot]


class ContactOption(BaseModel):
    """A contact discovered from the bridge DB. Used to populate the
    'Add rule' contact dropdown so the user doesn't have to type JIDs."""
    contact_jid: str
    contact_phone: str | None
    name: str | None


# --- helpers ----------------------------------------------------------------

def _phone_from_jid(jid: str) -> str | None:
    """Pull the digits out of ``<digits>@s.whatsapp.net``. Returns None
    for the ``'*'`` wildcard or anything else that isn't a phone JID.
    """
    if not jid or jid == "*":
        return None
    if "@" not in jid:
        # Treat bare digits as a phone (lenient).
        return jid if jid.isdigit() else None
    local, host = jid.split("@", 1)
    if host != "s.whatsapp.net":
        return None
    return local if local.isdigit() else None


def _agents_by_id(db: Session, ids: list[int]) -> dict[int, Agent]:
    if not ids:
        return {}
    rows = db.query(Agent).filter(Agent.id.in_(ids)).all()
    return {a.id: a for a in rows}


# --- routes -----------------------------------------------------------------

@router.get("/{wa_login_id}/rules", response_model=WhatsAppRoutingOut)
async def list_routing(
    wa_login_id: str,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the routing rules + default bot + current portals for one WA number.

    Authorization: the caller must either own a bot bound to this number,
    OR have at least one routing rule of their own on this number. We
    don't expose other users' rules.
    """
    wa_login_id = wa_login_id.strip()
    if not wa_login_id or not wa_login_id.isdigit():
        raise HTTPException(400, "wa_login_id must be a numeric WA login id")

    # The "default" bot is the one with agents.whatsapp_login_id == this.
    # Scoped to the caller's own bots.
    default_agent: Agent | None = (
        db.query(Agent)
        .filter(Agent.owner_user_id == current.id,
                Agent.whatsapp_login_id == wa_login_id)
        .one_or_none()
    )

    # Routing rules owned by this user on this number.
    rules: list[WhatsAppRoutingRule] = (
        db.query(WhatsAppRoutingRule)
        .filter(WhatsAppRoutingRule.user_id == current.id,
                WhatsAppRoutingRule.wa_login_id == wa_login_id)
        .order_by(WhatsAppRoutingRule.priority, WhatsAppRoutingRule.id)
        .all()
    )

    # Authorization: at least one of (default bot, rules) must exist.
    if default_agent is None and not rules:
        raise HTTPException(
            404,
            "No bots or routing rules on this WA number for the current user.",
        )

    # Resolve agent names in one query.
    agent_ids = [r.agent_id for r in rules]
    if default_agent is not None:
        agent_ids.append(default_agent.id)
    agents = _agents_by_id(db, list(set(agent_ids)))

    # Bridge-side snapshot: portals on this number for this owner's MXID.
    portals: list[PortalSnapshot] = []
    cred = (
        db.query(WebMatrixCredential)
        .filter(WebMatrixCredential.user_id == current.id)
        .one_or_none()
    )
    if cred is not None and bridge_db.configured:
        try:
            bridge_portals = bridge_db.list_portals_for_login(
                cred.matrix_user_id, wa_login_id, room_types=["dm"],
            )
        except Exception:
            log.exception("bridge portal lookup failed for %s × %s",
                          cred.matrix_user_id, wa_login_id)
            bridge_portals = []
        for p in bridge_portals:
            if not p.mxid:
                continue
            portals.append(PortalSnapshot(
                contact_jid=p.portal_id,
                contact_phone=_phone_from_jid(p.portal_id),
                portal_mxid=p.mxid,
                portal_name=p.name or _phone_from_jid(p.portal_id) or p.portal_id,
            ))

    return WhatsAppRoutingOut(
        wa_login_id=wa_login_id,
        default_bot=(
            DefaultBotOut(
                agent_id=default_agent.id,
                agent_name=default_agent.display_name,
            ) if default_agent is not None else None
        ),
        rules=[
            RoutingRuleOut(
                id=r.id,
                wa_login_id=r.wa_login_id,
                contact_jid=r.contact_jid,
                contact_phone=_phone_from_jid(r.contact_jid),
                agent_id=r.agent_id,
                agent_name=(
                    agents[r.agent_id].display_name
                    if r.agent_id in agents
                    else f"(missing agent #{r.agent_id})"
                ),
                priority=r.priority,
            )
            for r in rules
        ],
        portals=portals,
    )


@router.get("/{wa_login_id}/contacts", response_model=list[ContactOption])
async def list_contacts(
    wa_login_id: str,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List known WA contacts for this number (read from bridge DB).

    Used to populate the 'Add rule' contact dropdown. Authorization
    follows the same rule as ``list_routing``: caller must either own a
    bot bound here or have a routing rule on the number.
    """
    wa_login_id = wa_login_id.strip()
    if not wa_login_id or not wa_login_id.isdigit():
        raise HTTPException(400, "wa_login_id must be a numeric WA login id")

    # Same authorization check as the rules endpoint.
    has_default = db.query(Agent).filter(
        Agent.owner_user_id == current.id,
        Agent.whatsapp_login_id == wa_login_id,
    ).first() is not None
    has_rule = db.query(WhatsAppRoutingRule).filter(
        WhatsAppRoutingRule.user_id == current.id,
        WhatsAppRoutingRule.wa_login_id == wa_login_id,
    ).first() is not None
    if not (has_default or has_rule):
        raise HTTPException(404, "No bots or routing rules on this WA number.")

    cred = (
        db.query(WebMatrixCredential)
        .filter(WebMatrixCredential.user_id == current.id)
        .one_or_none()
    )
    if cred is None or not bridge_db.configured:
        return []

    try:
        bridge_portals = bridge_db.list_portals_for_login(
            cred.matrix_user_id, wa_login_id, room_types=["dm"],
        )
    except Exception:
        log.exception("bridge contacts lookup failed")
        return []

    out: list[ContactOption] = []
    seen: set[str] = set()
    for p in bridge_portals:
        if p.portal_id in seen:
            continue
        seen.add(p.portal_id)
        out.append(ContactOption(
            contact_jid=p.portal_id,
            contact_phone=_phone_from_jid(p.portal_id),
            name=p.name or None,
        ))
    return out
