"""Direct read access to the mautrix-whatsapp bridge's Postgres database.

The bridge's HTTP provisioning API doesn't (currently, as of v26.04) expose
a "list this user's portal rooms" endpoint, so we query its DB directly.
This is a pragmatic shortcut — the alternative would be running our own
Matrix sync loop, which is significantly more work and duplicates state
that the bridge already maintains.

Tradeoff: this couples us to the bridge's schema. If they break it on a
future upgrade we'll need to update the queries here. We mitigate by:
  - Using only the most stable tables (`portal`, `user_portal`).
  - Read-only access (we never write).
  - Catching all exceptions at the route layer (UI degrades gracefully).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


@dataclass
class BridgePortal:
    """One bridged room as the bridge sees it."""
    portal_id: str          # e.g. "66909966651@s.whatsapp.net" (WA JID)
    receiver: str           # the user's WA number, e.g. "66939218159"
    mxid: str | None        # the Matrix room id, or None if not yet created
    name: str
    room_type: str          # "dm", "group", "space", ...
    in_space: bool          # whether it's been added to the user's WA space


def _connection_kwargs_from_url(url: str) -> dict:
    """Parse a postgres:// URL into psycopg connect kwargs.

    We don't import sqlalchemy.engine.URL here to keep this module self-contained.
    psycopg accepts the URL directly, so we mostly just pass it through.
    """
    return {"conninfo": url}


def _read_bridge_db_url() -> str | None:
    """Resolve the bridge's Postgres URL.

    Priority:
      1. Env var ADMIN_WHATSAPP_BRIDGE_DB_URL (explicit override)
      2. The `database.uri` field in the bridge's config.yaml, if readable
    """
    explicit = os.environ.get("ADMIN_WHATSAPP_BRIDGE_DB_URL", "").strip()
    if explicit:
        return explicit

    # Try the bridge's config.yaml — same host, well-known path.
    cfg_path = Path("/home/bots/mautrix-whatsapp/config.yaml")
    if not cfg_path.is_file():
        return None
    try:
        # Tiny scan rather than importing PyYAML here; works because the URI
        # is a single line in `database:` block.
        with cfg_path.open("r", encoding="utf-8") as f:
            in_db = False
            for line in f:
                stripped = line.strip()
                if stripped.startswith("database:"):
                    in_db = True
                    continue
                if in_db:
                    if line.startswith(" ") or line.startswith("\t"):
                        if stripped.startswith("uri:"):
                            return stripped.split(":", 1)[1].strip()
                    else:
                        in_db = False
    except OSError:
        return None
    return None


class WhatsAppBridgeDB:
    """Lazy, lightweight client. Opens a connection per query, closes it after.

    The query volume is tiny (a few per page load), so connection pooling
    isn't worth the complexity. psycopg's autocommit + short-lived
    connections are perfectly fine here.
    """

    def __init__(self, db_url: str | None = None):
        self.db_url = db_url or _read_bridge_db_url()

    @property
    def configured(self) -> bool:
        return bool(self.db_url)

    def list_portals_for_login(
        self,
        user_mxid: str,
        login_id: str,
        *,
        room_types: Iterable[str] | None = None,
    ) -> list[BridgePortal]:
        """Return the portal rooms bridged for ``user_mxid`` × ``login_id``.

        ``room_types`` filters by ``portal.room_type``. Default = all types.
        Only rows with a non-null ``mxid`` are returned (some portals may be
        recorded by the bridge before the Matrix room is actually created).
        """
        if not self.db_url:
            return []

        sql = """
            SELECT p.id, p.receiver, p.mxid, p.name, p.room_type, p.in_space
            FROM portal p
            JOIN user_portal up
              ON up.portal_id      = p.id
             AND up.portal_receiver = p.receiver
            WHERE up.user_mxid = %s
              AND up.login_id  = %s
              AND p.mxid IS NOT NULL
        """
        params: list = [user_mxid, login_id]
        if room_types is not None:
            types = tuple(room_types)
            if not types:
                return []
            sql += " AND p.room_type = ANY(%s)"
            params.append(list(types))
        sql += " ORDER BY p.name, p.id"

        try:
            with psycopg.connect(self.db_url, autocommit=True, row_factory=dict_row) as conn:
                rows = conn.execute(sql, params).fetchall()
        except psycopg.Error as e:
            log.warning("bridge DB query failed: %s", e)
            return []

        return [BridgePortal(
            portal_id=r["id"],
            receiver=r["receiver"],
            mxid=r["mxid"],
            name=r["name"] or "",
            room_type=r["room_type"],
            in_space=bool(r["in_space"]),
        ) for r in rows]


# Module-level singleton — resolves lazily so unit tests can monkeypatch.
bridge_db = WhatsAppBridgeDB()
