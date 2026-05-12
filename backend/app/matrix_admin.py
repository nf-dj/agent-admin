"""Small Matrix (Synapse) admin client.

Handles:
- Logging in as the configured admin user (caches access token).
- Creating new users via the Synapse admin API.
- Logging in as a freshly created user to obtain that user's access token.
- Deactivating users (for cleanup on agent deletion).

This client is intentionally minimal — only what we need to provision and
de-provision per-agent Matrix accounts. Uses stdlib http.client to avoid an
extra dependency.
"""
from __future__ import annotations
import json
import logging
import re
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass

from .config import settings


log = logging.getLogger(__name__)


class MatrixError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class MatrixUser:
    user_id: str        # full mxid, e.g. @bot_u1-foo:matrix.netforce.com
    localpart: str      # e.g. bot_u1-foo
    access_token: str
    device_id: str
    homeserver: str
    password: str       # plaintext (we generated it); caller may want to store for re-login


class MatrixAdmin:
    """Stateful admin client. Lazily logs in; refreshes on auth failures."""

    def __init__(self):
        self._admin_token: str | None = None
        self._token_obtained_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(settings.matrix_enabled
                    and settings.matrix_homeserver
                    and settings.matrix_server_name
                    and settings.matrix_admin_user
                    and settings.matrix_admin_password)

    @property
    def homeserver(self) -> str:
        return settings.matrix_homeserver.rstrip("/")

    @property
    def server_name(self) -> str:
        return settings.matrix_server_name

    # ---------- HTTP helpers ----------
    def _request(self, method: str, path: str, *, body: dict | None = None,
                 token: str | None = None, timeout: int = 20) -> dict:
        url = f"{self.homeserver}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise MatrixError(f"Matrix {method} {path} failed: {e.code} {err_body}",
                              status=e.code, body=err_body)
        except urllib.error.URLError as e:
            raise MatrixError(f"Matrix {method} {path} failed: {e}", status=None)
        try:
            return json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            return {"_raw": payload}

    # ---------- admin auth ----------
    def _login_admin(self) -> str:
        body = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": settings.matrix_admin_user},
            "password": settings.matrix_admin_password,
            "initial_device_display_name": "agent-admin",
        }
        data = self._request("POST", "/_matrix/client/v3/login", body=body)
        token = data.get("access_token")
        if not token:
            raise MatrixError(f"login did not return access_token: {data}")
        self._admin_token = token
        self._token_obtained_at = time.time()
        log.info("Matrix admin logged in as %s", data.get("user_id"))
        return token

    def admin_token(self) -> str:
        if self._admin_token is None:
            self._login_admin()
        assert self._admin_token is not None
        return self._admin_token

    def _admin_request(self, method: str, path: str, *, body: dict | None = None) -> dict:
        try:
            return self._request(method, path, body=body, token=self.admin_token())
        except MatrixError as e:
            if e.status in (401, 403):
                log.info("Matrix admin token rejected, re-logging in")
                self._admin_token = None
                return self._request(method, path, body=body, token=self.admin_token())
            raise

    # ---------- user provisioning ----------
    @staticmethod
    def sanitize_localpart(s: str) -> str:
        """Matrix localparts allow a-z 0-9 . _ = - / +; we use a conservative subset."""
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9._=/+-]", "-", s)
        return s.strip("-") or "bot"

    def _build_user_id(self, localpart: str) -> str:
        return f"@{localpart}:{self.server_name}"

    def create_user(self, localpart_hint: str, *, display_name: str | None = None) -> MatrixUser:
        """Provision a fresh Matrix user. Returns its access token.

        Idempotent on conflict — if the user already exists we just log in to it
        using the stored password (caller is expected to pass a fresh hint when
        a fresh account is required).
        """
        localpart = self.sanitize_localpart(f"{settings.matrix_user_prefix}{localpart_hint}")
        user_id = self._build_user_id(localpart)
        password = secrets.token_urlsafe(24)

        # Create / overwrite (PUT is idempotent on this endpoint)
        body: dict = {
            "password": password,
            "admin": False,
            "deactivated": False,
        }
        if display_name:
            body["displayname"] = display_name
        encoded = urllib.parse.quote(user_id, safe="")
        self._admin_request("PUT", f"/_synapse/admin/v2/users/{encoded}", body=body)

        # Log the new user in to grab an access token
        login = self._request("POST", "/_matrix/client/v3/login", body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": password,
            "initial_device_display_name": "agent-admin/openclaw",
        })
        access_token = login.get("access_token")
        device_id = login.get("device_id", "")
        if not access_token:
            raise MatrixError(f"bot user login did not return access_token: {login}")

        log.info("Provisioned Matrix user %s (device=%s)", user_id, device_id)
        return MatrixUser(
            user_id=user_id,
            localpart=localpart,
            access_token=access_token,
            device_id=device_id,
            homeserver=self.homeserver,
            password=password,
        )

    def create_web_user(self, localpart_hint: str, *, display_name: str | None = None) -> MatrixUser:
        """Provision a Matrix user for a web chat session.

        Same machinery as `create_user` but with a `web_` prefix so the
        agent-admin UI's accounts are easy to distinguish from agent bots
        (`bot_*`). The localpart_hint is e.g. `u<userId>` so each app user
        deterministically maps to the same matrix account.
        """
        localpart = self.sanitize_localpart(f"web_{localpart_hint}")
        user_id = self._build_user_id(localpart)
        password = secrets.token_urlsafe(24)

        body: dict = {
            "password": password,
            "admin": False,
            "deactivated": False,
        }
        if display_name:
            body["displayname"] = display_name
        encoded = urllib.parse.quote(user_id, safe="")
        self._admin_request("PUT", f"/_synapse/admin/v2/users/{encoded}", body=body)

        login = self._request("POST", "/_matrix/client/v3/login", body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": password,
            "initial_device_display_name": "agent-admin/web",
        })
        access_token = login.get("access_token")
        device_id = login.get("device_id", "")
        if not access_token:
            raise MatrixError(f"web user login did not return access_token: {login}")

        log.info("Provisioned web Matrix user %s (device=%s)", user_id, device_id)
        return MatrixUser(
            user_id=user_id,
            localpart=localpart,
            access_token=access_token,
            device_id=device_id,
            homeserver=self.homeserver,
            password=password,
        )

    def login_user(self, localpart: str, password: str) -> dict:
        """Re-mint an access token for an existing user. Returns the raw login response."""
        return self._request("POST", "/_matrix/client/v3/login", body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": password,
            "initial_device_display_name": "agent-admin/web",
        })

    def deactivate_user(self, user_id: str) -> None:
        """Deactivate (and erase) a bot user on the homeserver."""
        encoded = urllib.parse.quote(user_id, safe="")
        try:
            self._admin_request(
                "POST",
                f"/_synapse/admin/v1/deactivate/{encoded}",
                body={"erase": True},
            )
            log.info("Deactivated Matrix user %s", user_id)
        except MatrixError as e:
            if e.status == 404:
                log.info("Matrix user %s already gone", user_id)
                return
            raise

    def set_display_name(self, user_id: str, name: str) -> None:
        encoded = urllib.parse.quote(user_id, safe="")
        self._admin_request("PUT", f"/_synapse/admin/v2/users/{encoded}",
                            body={"displayname": name})

    def invite_user_to_room(
        self,
        room_id: str,
        invitee_mxid: str,
        *,
        inviter_token: str,
    ) -> bool:
        """Invite ``invitee_mxid`` to ``room_id`` using the inviter's token.

        Returns True on success or if the user was already in the room.
        Raises :class:`MatrixError` for other failures.

        The inviter must be a member of the room with sufficient power level
        (default: power_level >= invite). For mautrix-whatsapp portal rooms
        the user who paired the WA account is admin, so their stored web-
        Matrix token works fine.
        """
        encoded_room = urllib.parse.quote(room_id, safe="")
        try:
            self._request(
                "POST",
                f"/_matrix/client/v3/rooms/{encoded_room}/invite",
                body={"user_id": invitee_mxid},
                token=inviter_token,
            )
            return True
        except MatrixError as e:
            # Common idempotent cases: already in the room (403 M_FORBIDDEN with
            # a 'is already in the room' message). Treat as success.
            if e.status == 403 and "already in the room" in (e.body or "").lower():
                return True
            raise

    def accept_pending_invites(self, access_token: str, user_id: str) -> int:
        """Auto-join any rooms the user has been invited to, and tag them as DMs.

        Returns the number of rooms joined. Safe to call repeatedly — only
        looks at currently-pending invites. Use this after provisioning a bot
        user so it's reachable as soon as someone DMs it.

        Tagging the room in `m.direct` account_data is required for
        OpenClaw's `dm.policy` to treat the room as a DM (Element creates
        the room client-side but the invitee must mark it as a DM on its own
        account).
        """
        try:
            sync = self._request("GET", "/_matrix/client/v3/sync?timeout=0", token=access_token)
            invites = (sync.get("rooms", {}) or {}).get("invite", {}) or {}
        except MatrixError as e:
            log.warning("accept_pending_invites: sync failed: %s", e)
            return 0

        joined = 0
        new_dms: dict[str, list[str]] = {}
        for room_id, room_data in invites.items():
            encoded_room = urllib.parse.quote(room_id, safe="")
            try:
                self._request("POST", f"/_matrix/client/v3/rooms/{encoded_room}/join",
                              body={}, token=access_token)
                joined += 1
                log.info("Accepted Matrix invite to %s", room_id)
            except MatrixError as e:
                log.warning("Failed to join %s: %s", room_id, e)
                continue

            # Figure out the inviter so we can tag the room as a DM with them.
            inviter = self._inviter_from_invite_state(room_data)
            if inviter:
                new_dms.setdefault(inviter, []).append(room_id)

        if new_dms:
            self._merge_m_direct(access_token, user_id, new_dms)
        return joined

    @staticmethod
    def _inviter_from_invite_state(room_data: dict) -> str | None:
        events = (room_data.get("invite_state", {}) or {}).get("events", []) or []
        for e in events:
            if e.get("type") == "m.room.member" and e.get("content", {}).get("membership") == "invite":
                # `sender` on the invite member event is the person who invited us
                inviter = e.get("sender")
                if inviter:
                    return inviter
        return None

    def _merge_m_direct(self, access_token: str, user_id: str,
                        new_dms: dict[str, list[str]]) -> None:
        """Merge new {peer -> [room_ids]} mappings into the bot's m.direct."""
        encoded_user = urllib.parse.quote(user_id, safe="")
        try:
            existing = self._request(
                "GET", f"/_matrix/client/v3/user/{encoded_user}/account_data/m.direct",
                token=access_token)
        except MatrixError as e:
            if e.status == 404:
                existing = {}
            else:
                log.warning("Could not read m.direct for %s: %s", user_id, e)
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        for peer, rooms in new_dms.items():
            current = list(existing.get(peer, []) or [])
            for r in rooms:
                if r not in current:
                    current.append(r)
            existing[peer] = current
        try:
            self._request(
                "PUT", f"/_matrix/client/v3/user/{encoded_user}/account_data/m.direct",
                body=existing, token=access_token)
            log.info("Updated m.direct for %s with %d peers", user_id, len(new_dms))
        except MatrixError as e:
            log.warning("Could not write m.direct for %s: %s", user_id, e)


# Singleton — fine because settings are immutable per-process.
matrix_admin = MatrixAdmin()
