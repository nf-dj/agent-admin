"""User-facing routes for the mautrix-whatsapp bridge integration.

Endpoints:
  GET    /api/me/whatsapp/status         — is the bridge configured? what mxid will be used?
  GET    /api/me/whatsapp/logins         — list user's paired WA accounts
  POST   /api/me/whatsapp/login/start    — begin a pairing session (flow_id in body)
  POST   /api/me/whatsapp/login/step     — drive the next step (long-polled for QR rotation)
  DELETE /api/me/whatsapp/logins/{login_id}  — unlink a WA account

For QR pairing the flow is:
  1. POST /login/start with {"flow_id":"qr"}
     → returns first step: { login_id, step_id, type: "display_and_wait",
                              display_and_wait: { type: "qr", data: "2@…" } }
  2. Render the QR client-side and POST /login/step with the same login_id /
     step_id / action="display_and_wait". This is a long-poll that returns
     when:
        • The QR rotates (returns a new display_and_wait step with new data)
        • The user scans (returns a different step type, possibly "complete")
        • A timeout occurs (client should retry)
  3. Repeat until the response indicates completion.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import get_current_user
from .db import SessionLocal, User
from .whatsapp_client import WhatsAppBridgeError
from .routes_me import _ensure_creds  # reuse the lazy mxid provisioner

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/me/whatsapp", tags=["whatsapp"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _bridge_or_503():
    """Late import + 503 if the bridge isn't configured.

    The bridge client lives on app.state — set by lifespan in main.py.
    Looked up via a closure import to avoid a circular dep.
    """
    from .main import app  # noqa: WPS433 — runtime to dodge circular import
    bridge = getattr(app.state, "whatsapp_bridge", None)
    if bridge is None:
        raise HTTPException(
            status_code=503,
            detail="WhatsApp bridge is not configured on this server.",
        )
    return bridge


# --- schemas -----------------------------------------------------------------

class WhatsAppStatusOut(BaseModel):
    configured: bool
    mxid: str | None = None
    flows: list[dict[str, Any]] = []


class WhatsAppLoginOut(BaseModel):
    """A linked WA account (one row per linked number)."""
    id: str                       # bridge's login_id
    name: str | None = None       # display name from the bridge (often the phone number)
    profile: dict[str, Any] | None = None
    state: dict[str, Any] | None = None


class LoginStartIn(BaseModel):
    flow_id: str = Field(..., examples=["qr", "phone"])


class LoginStepIn(BaseModel):
    login_id: str
    step_id: str
    action: str = "display_and_wait"
    payload: dict[str, Any] | None = None


# --- helpers -----------------------------------------------------------------

def _mxid_for(user: User, db) -> str:
    cred = _ensure_creds(db, user)
    return cred.matrix_user_id


# --- endpoints ---------------------------------------------------------------

@router.get("/status", response_model=WhatsAppStatusOut)
async def status(current: User = Depends(get_current_user), db=Depends(get_db)):
    """Report whether the bridge is reachable + the mxid used for auth."""
    from .main import app
    bridge = getattr(app.state, "whatsapp_bridge", None)
    if bridge is None:
        return WhatsAppStatusOut(configured=False)

    mxid = _mxid_for(current, db)
    try:
        flows = await bridge.list_login_flows(mxid)
    except WhatsAppBridgeError as e:
        log.warning("bridge unreachable for status check: %s", e)
        return WhatsAppStatusOut(configured=False, mxid=mxid)

    return WhatsAppStatusOut(configured=True, mxid=mxid, flows=flows)


@router.get("/logins", response_model=list[WhatsAppLoginOut])
async def list_logins(current: User = Depends(get_current_user), db=Depends(get_db)):
    bridge = _bridge_or_503()
    mxid = _mxid_for(current, db)
    try:
        rows = await bridge.list_logins(mxid)
    except WhatsAppBridgeError as e:
        raise HTTPException(status_code=502, detail=f"Bridge error: {e}")

    out: list[WhatsAppLoginOut] = []
    for r in rows:
        if isinstance(r, str):
            # Older API returns just an id list.
            out.append(WhatsAppLoginOut(id=r))
            continue
        rid = r.get("id") or r.get("login_id")
        if not rid:
            continue
        out.append(WhatsAppLoginOut(
            id=rid,
            name=(r.get("remote_name")
                  or (r.get("profile") or {}).get("name")
                  or (r.get("profile") or {}).get("remote_name")),
            profile=r.get("profile"),
            state=r.get("state"),
        ))
    return out


@router.post("/login/start")
async def login_start(body: LoginStartIn,
                      current: User = Depends(get_current_user),
                      db=Depends(get_db)):
    bridge = _bridge_or_503()
    mxid = _mxid_for(current, db)
    try:
        return await bridge.start_login(mxid, body.flow_id)
    except WhatsAppBridgeError as e:
        raise HTTPException(status_code=e.status_code or 502,
                            detail=f"Bridge error: {e}")


@router.post("/login/step")
async def login_step(body: LoginStepIn,
                     current: User = Depends(get_current_user),
                     db=Depends(get_db)):
    """Drive the next step of an active pairing session.

    This may long-poll for up to ~90s. The browser should set a matching
    fetch timeout and re-issue on disconnect.
    """
    bridge = _bridge_or_503()
    mxid = _mxid_for(current, db)
    try:
        # Use shield so a cancelled request doesn't tear down the bridge's
        # in-flight whatsmeow operation mid-pair.
        coro = bridge.step_action(
            mxid, body.login_id, body.step_id, body.action,
            payload=body.payload, timeout=90.0,
        )
        return await asyncio.wait_for(coro, timeout=95.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504,
                            detail="Bridge step timed out; retry the call.")
    except WhatsAppBridgeError as e:
        raise HTTPException(status_code=e.status_code or 502,
                            detail=f"Bridge error: {e}")


@router.delete("/logins/{login_id}")
async def delete_login(login_id: str,
                       current: User = Depends(get_current_user),
                       db=Depends(get_db)):
    bridge = _bridge_or_503()
    mxid = _mxid_for(current, db)
    try:
        await bridge.logout(mxid, login_id)
    except WhatsAppBridgeError as e:
        raise HTTPException(status_code=e.status_code or 502,
                            detail=f"Bridge error: {e}")

    # Also detach any bots in this user's account that were pinned to this login.
    from .db import Agent
    bots = db.query(Agent).filter(
        Agent.owner_user_id == current.id,
        Agent.whatsapp_login_id == login_id,
    ).all()
    for b in bots:
        b.whatsapp_login_id = None
        db.add(b)
    if bots:
        db.commit()

    return {"ok": True, "detached_bots": len(bots)}
