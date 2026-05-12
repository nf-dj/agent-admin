"""HTTP client for the mautrix-whatsapp bridge's provisioning API.

The bridge exposes a clean, versioned HTTP API for managing WA logins on
behalf of any Matrix user (we authenticate with a single shared secret,
then act per-user via the `?user_id=` query param).

Docs: https://docs.mau.fi/bridges/general/provisioning-api.html
The endpoints we use:

  GET    /_matrix/provision/v3/login/flows?user_id=@x:host
  POST   /_matrix/provision/v3/login/start/{flow_id}?user_id=…
  POST   /_matrix/provision/v3/login/step/{login_id}/{step_id}/{action}?user_id=…
  GET    /_matrix/provision/v3/logins?user_id=…
  DELETE /_matrix/provision/v3/logout/{login_id}?user_id=…

This client does NOT poll on its own — the FastAPI route layer drives the
long-poll, so it can be cancelled if the user closes the browser.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class WhatsAppBridgeConfig:
    base_url: str           # e.g. "http://127.0.0.1:29318"
    shared_secret: str      # from mautrix-whatsapp config.yaml provisioning.shared_secret

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.shared_secret)


class WhatsAppBridgeError(Exception):
    """Raised when the bridge returns a non-2xx response or is unreachable."""

    def __init__(self, message: str, *, status_code: int | None = None, errcode: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.errcode = errcode


class WhatsAppBridge:
    """Thin async wrapper around the mautrix-whatsapp provisioning API.

    One instance is shared by the FastAPI app (created at startup, closed
    at shutdown). Per-request, we pass `mxid` to identify the acting user.
    """

    def __init__(self, config: WhatsAppBridgeConfig):
        self.config = config
        # Long-ish default timeout; long-poll wait calls override per-request.
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0),
            headers={"Authorization": f"Bearer {config.shared_secret}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- internal helpers ----------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        mxid: str | None,
        json: Any = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        params: dict[str, str] = {}
        if mxid:
            params["user_id"] = mxid
        try:
            resp = await self._client.request(
                method,
                f"/_matrix/provision/v3{path}",
                params=params,
                json=json,
                timeout=timeout if timeout is not None else self._client.timeout,
            )
        except httpx.HTTPError as e:
            raise WhatsAppBridgeError(f"bridge unreachable: {e!s}")

        if resp.status_code == 204 or not resp.content:
            return None

        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code >= 400:
            errcode = (body or {}).get("errcode") if isinstance(body, dict) else None
            msg = (body or {}).get("error") if isinstance(body, dict) else resp.text
            raise WhatsAppBridgeError(
                msg or f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                errcode=errcode,
            )
        return body

    # ---------- login flows ----------

    async def list_login_flows(self, mxid: str) -> list[dict[str, Any]]:
        body = await self._request("GET", "/login/flows", mxid=mxid)
        return (body or {}).get("flows", []) if isinstance(body, dict) else []

    async def start_login(self, mxid: str, flow_id: str) -> dict[str, Any]:
        """Begin a pairing session. Returns the first step (usually display_and_wait with QR or phone code prompt)."""
        body = await self._request("POST", f"/login/start/{flow_id}", mxid=mxid)
        if not isinstance(body, dict):
            raise WhatsAppBridgeError("unexpected start_login response shape")
        return body

    async def step_action(
        self,
        mxid: str,
        login_id: str,
        step_id: str,
        action: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 90.0,
    ) -> dict[str, Any]:
        """Perform a step action.

        For display_and_wait steps, action="display_and_wait" is a long-poll
        that returns when the displayed value changes (QR rotates) or the
        user advances to the next step (scans the QR).

        For user_input steps (phone-code flow), action="user_input" with
        payload={"phone_number": "..."} returns the next step.
        """
        body = await self._request(
            "POST",
            f"/login/step/{login_id}/{step_id}/{action}",
            mxid=mxid,
            json=payload or {},
            timeout=timeout,
        )
        if not isinstance(body, dict):
            raise WhatsAppBridgeError("unexpected step_action response shape")
        return body

    # ---------- session management ----------

    async def list_logins(self, mxid: str) -> list[dict[str, Any]]:
        """List the user's currently-linked WA accounts.

        Each entry includes login_id, remote_name (the WA phone number),
        and possibly profile / state details.
        """
        body = await self._request("GET", "/logins", mxid=mxid)
        if isinstance(body, dict):
            return body.get("login_ids") or body.get("logins") or []
        return []

    async def logout(self, mxid: str, login_id: str) -> None:
        await self._request("DELETE", f"/logout/{login_id}", mxid=mxid)

    async def logout_all(self, mxid: str) -> None:
        await self._request("DELETE", "/logout/all", mxid=mxid)
