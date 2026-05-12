"""WhatsApp per-contact routing rules.

A WA number can be shared across multiple bots: the existing
``agents.whatsapp_login_id`` column holds the *default* bot, and this
module manages contact-specific overrides via the ``wa_routing_rules``
table.

Endpoints:
  GET    /api/whatsapp/numbers/{wa_login_id}/rules
    → Default bot + rules + portal snapshot with resolved routing.
  GET    /api/whatsapp/numbers/{wa_login_id}/contacts
    → Known WA contacts on this number (for the rule form dropdown).
  POST   /api/whatsapp/numbers/{wa_login_id}/rules
    → Create or update a routing rule (upsert on contact_jid).
  DELETE /api/whatsapp/numbers/{wa_login_id}/rules/{rule_id}
    → Remove a routing rule.

Owner-only. ``wa_login_id`` is the bare numeric login (e.g. ``66939218159``),
not the full JID.

Phase 3 will add the worker that walks portals and applies these rules.
For now the rules are purely declarative — saving one doesn't change any
portal memberships.
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


class SubscriberOut(BaseModel):
    """One bot that has subscribed to this WA number
    (i.e. ``agents.whatsapp_login_id == this``).

    Multiple bots can share a number; the routing rules decide who
    answers each contact. The first subscriber (when there's no
    ``contact_jid='*'`` fallback rule and no per-contact rule matches)
    is the implicit default; see ``WhatsAppRoutingOut.default_bot``.
    """
    agent_id: int
    agent_name: str


class DefaultBotOut(BaseModel):
    """The resolved fallback bot.

    Priority:
      1. The ``contact_jid='*'`` rule (explicit fallback).
      2. If there's exactly **one** subscriber, that subscriber.
      3. Otherwise null — the UI warns the user to set an explicit
         fallback rule, since with multiple subscribers and no rule
         we can't pick deterministically.
    """
    agent_id: int
    agent_name: str
    source: str   # "rule" | "sole-subscriber"


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
    # All of the caller's bots that have subscribed to this number
    # (i.e. set ``whatsapp_login_id``). Ordered by agent id.
    subscribers: list[SubscriberOut]
    # Resolved fallback bot — see DefaultBotOut docstring. Null if
    # ambiguous (multiple subscribers, no '*' rule).
    default_bot: DefaultBotOut | None
    rules: list[RoutingRuleOut]
    portals: list[PortalSnapshot]


class ContactOption(BaseModel):
    """A contact discovered from the bridge DB. Used to populate the
    'Add rule' contact dropdown so the user doesn't have to type JIDs."""
    contact_jid: str
    contact_phone: str | None
    name: str | None


class RoutingRuleIn(BaseModel):
    """Body for POST .../rules. ``contact`` can be:
      - ``"*"`` for the fallback rule,
      - a bare phone like ``"66909966651"`` or ``"+66 90 996 6651"``,
      - or a full JID like ``"66909966651@s.whatsapp.net"``.
    """
    contact: str
    agent_id: int
    priority: int = 100


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


def _canonical_contact(raw: str) -> str:
    """Normalize a user-supplied contact identifier to the canonical form
    we store in ``wa_routing_rules.contact_jid``.

    Returns either ``"*"`` or ``"<digits>@s.whatsapp.net"``. Raises
    ``HTTPException`` if the input can't be parsed as either.
    """
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "contact is required")
    if raw == "*":
        return "*"
    # Allow full JIDs as-is (after light normalisation).
    if "@" in raw:
        local, host = raw.split("@", 1)
        local = "".join(ch for ch in local if ch.isdigit())
        if host.lower() == "s.whatsapp.net" and local:
            return f"{local}@s.whatsapp.net"
        raise HTTPException(400, f"unsupported JID host {host!r}")
    # Strip everything that isn't a digit. Drop a leading 00 if present
    # (some users write "0066…" for international form).
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits:
        raise HTTPException(400, f"could not parse phone number from {raw!r}")
    if len(digits) < 6 or len(digits) > 20:
        raise HTTPException(400, f"phone number {digits!r} looks wrong (len={len(digits)})")
    return f"{digits}@s.whatsapp.net"


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

    # All bots the caller owns that are subscribed to this number.
    subscribers: list[Agent] = (
        db.query(Agent)
        .filter(Agent.owner_user_id == current.id,
                Agent.whatsapp_login_id == wa_login_id)
        .order_by(Agent.id)
        .all()
    )

    # Routing rules owned by this user on this number.
    rules: list[WhatsAppRoutingRule] = (
        db.query(WhatsAppRoutingRule)
        .filter(WhatsAppRoutingRule.user_id == current.id,
                WhatsAppRoutingRule.wa_login_id == wa_login_id)
        .order_by(WhatsAppRoutingRule.priority, WhatsAppRoutingRule.id)
        .all()
    )

    # Authorization: at least one of (subscribers, rules) must exist.
    if not subscribers and not rules:
        raise HTTPException(
            404,
            "No bots or routing rules on this WA number for the current user.",
        )

    # Resolve agent names in one query.
    agent_ids = [r.agent_id for r in rules] + [a.id for a in subscribers]
    agents = _agents_by_id(db, list(set(agent_ids)))

    # Resolve the fallback bot:
    #   1. explicit '*' rule wins;
    #   2. else, if exactly one subscriber, it's the implicit default;
    #   3. else null — ambiguous.
    star_rule = next((r for r in rules if r.contact_jid == "*"), None)
    if star_rule is not None and star_rule.agent_id in agents:
        default_bot_out = DefaultBotOut(
            agent_id=star_rule.agent_id,
            agent_name=agents[star_rule.agent_id].display_name,
            source="rule",
        )
    elif len(subscribers) == 1:
        default_bot_out = DefaultBotOut(
            agent_id=subscribers[0].id,
            agent_name=subscribers[0].display_name,
            source="sole-subscriber",
        )
    else:
        default_bot_out = None

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
        subscribers=[
            SubscriberOut(agent_id=a.id, agent_name=a.display_name)
            for a in subscribers
        ],
        default_bot=default_bot_out,
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


@router.post("/{wa_login_id}/rules", response_model=WhatsAppRoutingOut)
async def upsert_routing_rule(
    wa_login_id: str,
    body: RoutingRuleIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update a routing rule on this WA number.

    Uniqueness key is ``(user_id, wa_login_id, contact_jid)`` — posting a
    second rule for the same contact silently replaces the first. This
    matches users' mental model ("set the routing for this contact")
    better than rejecting duplicates with a 409.
    """
    wa_login_id = wa_login_id.strip()
    if not wa_login_id or not wa_login_id.isdigit():
        raise HTTPException(400, "wa_login_id must be a numeric WA login id")

    # Authorization: the user must own a bot bound to this number OR
    # already have a rule here. Without this anyone could pollute the
    # table with rules for numbers they don't pair.
    owns_default = db.query(Agent).filter(
        Agent.owner_user_id == current.id,
        Agent.whatsapp_login_id == wa_login_id,
    ).first() is not None
    owns_rule = db.query(WhatsAppRoutingRule).filter(
        WhatsAppRoutingRule.user_id == current.id,
        WhatsAppRoutingRule.wa_login_id == wa_login_id,
    ).first() is not None
    if not (owns_default or owns_rule):
        raise HTTPException(403, "You don't own this WA number.")

    # The target agent must be one of the caller's own bots.
    agent = db.query(Agent).filter(
        Agent.id == body.agent_id,
        Agent.owner_user_id == current.id,
    ).one_or_none()
    if agent is None:
        raise HTTPException(404, f"Agent #{body.agent_id} not found or not yours.")
    if not agent.matrix_user_id:
        raise HTTPException(
            400,
            f"Agent '{agent.display_name}' has no Matrix account — it can't be "
            "routed WhatsApp messages yet. Enable Matrix integration first.",
        )

    contact_jid = _canonical_contact(body.contact)

    # Upsert.
    existing = db.query(WhatsAppRoutingRule).filter(
        WhatsAppRoutingRule.user_id == current.id,
        WhatsAppRoutingRule.wa_login_id == wa_login_id,
        WhatsAppRoutingRule.contact_jid == contact_jid,
    ).one_or_none()
    if existing is None:
        rule = WhatsAppRoutingRule(
            user_id=current.id,
            wa_login_id=wa_login_id,
            contact_jid=contact_jid,
            agent_id=agent.id,
            priority=body.priority,
        )
        db.add(rule)
        log.info(
            "wa-routing: user %s created rule %s -> agent %s on %s",
            current.id, contact_jid, agent.id, wa_login_id,
        )
    else:
        existing.agent_id = agent.id
        existing.priority = body.priority
        log.info(
            "wa-routing: user %s updated rule %s -> agent %s on %s",
            current.id, contact_jid, agent.id, wa_login_id,
        )
    db.commit()

    return await list_routing(wa_login_id, current=current, db=db)


@router.delete("/{wa_login_id}/rules/{rule_id}", response_model=WhatsAppRoutingOut)
async def delete_routing_rule(
    wa_login_id: str,
    rule_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a routing rule. Returns the fresh state for the number."""
    rule = db.query(WhatsAppRoutingRule).filter(
        WhatsAppRoutingRule.id == rule_id,
        WhatsAppRoutingRule.user_id == current.id,
        WhatsAppRoutingRule.wa_login_id == wa_login_id,
    ).one_or_none()
    if rule is None:
        raise HTTPException(404, "Rule not found.")
    db.delete(rule)
    db.commit()
    log.info(
        "wa-routing: user %s deleted rule #%s (%s on %s)",
        current.id, rule_id, rule.contact_jid, wa_login_id,
    )
    return await list_routing(wa_login_id, current=current, db=db)
