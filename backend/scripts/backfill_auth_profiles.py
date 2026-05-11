"""One-shot backfill: push SQLite key state to OpenClaw per-agent profiles.

Walks every agent in the agent-admin DB, resolves the effective key for
every relevant provider (override → owner's user key → none), and writes
to ``~/.openclaw/agents/<id>/agent/auth-profiles.json`` and the global
``~/.openclaw/openclaw.json`` registry.

Idempotent. Safe to run anytime — only writes when state actually changes.

Run from the backend dir:
    .venv/bin/python -m scripts.backfill_auth_profiles
"""

from __future__ import annotations

import logging
import sys

from app.db import SessionLocal, init_db
from app.auth_sync import all_agents, sync_agent_all_providers


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("backfill")

    init_db()
    db = SessionLocal()
    try:
        agents = list(all_agents(db))
        log.info("Backfilling auth profiles for %d agent(s)", len(agents))
        for agent in agents:
            label = agent.harness_agent_id or f"<no-harness-id id={agent.id}>"
            log.info("  %s (owner_user_id=%s)", label, agent.owner_user_id)
            sync_agent_all_providers(db, agent)
    finally:
        db.close()

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
