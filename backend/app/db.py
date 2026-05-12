"""SQLAlchemy setup + models."""
from datetime import datetime
from sqlalchemy import (
    create_engine, event, String, Integer, DateTime, ForeignKey, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from .config import settings


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


# SQLite has ``ON DELETE CASCADE`` syntax in its DDL but ignores it unless
# the per-connection ``foreign_keys`` pragma is on (it defaults to off).
# Without this, deleting an Agent would silently leave orphaned
# ``agent_members`` rows behind, and since our PK is plain ``INTEGER
# PRIMARY KEY`` (not AUTOINCREMENT) the next insert can reuse the same id
# — then the orphan row collides on ``(agent_id, user_id)`` and the
# whole create blows up with a UNIQUE-constraint 500.
#
# Enable FKs on every new connection so cascades actually fire.
@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_conn, _conn_record):
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys = ON")
    finally:
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_admin: Mapped[int] = mapped_column(Integer, default=0)  # 0/1
    # Optional per-user prefix used when generating new agent ids.
    # If NULL/empty, the system falls back to ``u<user.id>-``.
    # Validated to be slug-safe (lowercase alnum + hyphens, <= 32 chars).
    company_prefix: Mapped[str | None] = mapped_column(String(32), nullable=True)

    agents: Mapped[list["Agent"]] = relationship(back_populates="owner", cascade="all,delete-orphan")


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("harness", "harness_agent_id", name="uq_harness_agent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Which harness this agent runs on. v1: "openclaw". Future: "hermes", ...
    harness: Mapped[str] = mapped_column(String(40), nullable=False, default="openclaw")

    # The id used inside the harness (e.g. openclaw agent id)
    harness_agent_id: Mapped[str] = mapped_column(String(120), nullable=False)

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    emoji: Mapped[str | None] = mapped_column(String(20), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Telegram bot integration (optional). Stored plaintext for now; can wrap with Fernet later.
    telegram_bot_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_account_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Matrix integration (optional, auto-provisioned by the admin user).
    matrix_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    matrix_access_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    matrix_device_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    matrix_account_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    matrix_password: Mapped[str | None] = mapped_column(String(120), nullable=True)

    workspace_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # WhatsApp bridge integration (optional). When set, the bot is wired
    # into all DM portal rooms for this user's WA login. The id is the
    # mautrix-whatsapp login_id (an opaque token from the bridge).
    whatsapp_login_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner: Mapped[User] = relationship(back_populates="agents")


class AgentMember(Base):
    """Membership of a user in an agent's access list.

    Every agent has at least one member: the owner (role='owner'). Owners
    can invite additional users with role='member'. Members can chat with
    the bot via the web UI but cannot read/modify settings, see API keys,
    invite others, or delete the agent.
    """
    __tablename__ = "agent_members"
    __table_args__ = (
        UniqueConstraint("agent_id", "user_id", name="uq_agent_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    invited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WebMatrixCredential(Base):
    """Per-user Matrix account used by the web chat UI.

    Each agent-admin User gets one auto-provisioned Matrix account
    (`@web_u<userId>:matrix.netforce.com`) on first chat. The browser uses
    these credentials with matrix-js-sdk to chat directly to bots via the
    homeserver. Token is plaintext for v1 (cheap to re-mint via admin API).
    """
    __tablename__ = "web_matrix_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    matrix_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    matrix_localpart: Mapped[str] = mapped_column(String(120), nullable=False)
    homeserver: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token: Mapped[str] = mapped_column(String(500), nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False)
    matrix_password: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentApiKey(Base):
    """Per-agent provider API key override, stored in plaintext.

    Not encrypted because:
      * The SQLite file lives next to ``~/.openclaw/ocplatform.json`` which
        already stores provider keys in plaintext under ``vars.*_API_KEY``.
        Encrypting in SQLite while plaintext in JSON would be theatre.
      * SQLite state is the source-of-truth that gets synced into the
        per-agent ``auth-profiles.json`` via ``openclaw models auth``.
        Storing ciphertext would force a decrypt step on every sync.

    Owner-only — members cannot read or set agent keys (enforced in routes).
    """
    __tablename__ = "agent_api_keys"
    __table_args__ = (
        UniqueConstraint("agent_id", "provider", name="uq_agent_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    key_preview: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserApiKey(Base):
    """Per-user provider API key, stored in plaintext.

    See :class:`AgentApiKey` for the rationale (same trust domain as
    ``openclaw.json``; SQLite is the source-of-truth that gets synced
    into the actual OpenClaw config).

    ``key_preview`` is a short non-secret hint (first 4 + last 4 chars,
    e.g. ``sk-1…2xyz``) so the UI can show what's stored without echoing
    the full key back — still useful for the casual "which one is this?"
    check, even though it's not protecting much in this trust domain.

    Provider names are the same identifiers used in ``openclaw.json``
    (``deepseek``, ``openai``, ``openrouter``, ``groq``, ``mistral``,
    ``anthropic``) plus the synthetic ``anthropic-subscription`` for Claude
    OAuth/subscription tokens.
    """
    __tablename__ = "user_api_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    api_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    key_preview: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomProvider(Base):
    """User-owned LLM provider config (BYO endpoint).

    Lets a user register their own model provider (e.g. a local llama-server
    reachable over Tailscale) so they can create agents that use it. Stored
    plaintext like ``UserApiKey``: same trust domain as ``openclaw.json``,
    which already keeps provider keys in the clear.

    ``slug`` is the per-user-unique short id (e.g. ``nucbox-llama``). At
    sync time we namespace it as ``u<userId>-<slug>`` when writing into
    ``openclaw.json`` so two users can both have a provider named
    ``nucbox-llama`` without colliding.

    ``models_json`` is the raw JSON array shape OpenClaw expects under
    ``models.providers.<id>.models`` — list of ``{id, name, reasoning,
    input, cost, contextWindow, maxTokens, compat}`` objects.
    """
    __tablename__ = "custom_providers"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_user_custom_provider_slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # OpenClaw's ``api`` field: openai-completions, openai-chat, anthropic, ...
    api_type: Mapped[str] = mapped_column(String(40), nullable=False, default="openai-completions")
    # Optional — local servers often don't require a key.
    api_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # JSON-encoded list of model definitions.
    models_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WhatsAppRoutingRule(Base):
    """Per-contact override of which bot answers on a shared WA number.

    The existing ``agents.whatsapp_login_id`` column already records the
    *default* bot for a WA login (one bot per number). This table layers
    contact-specific overrides on top: "messages from this WA contact go
    to bot X instead of the default".

    Matching is done by ``contact_jid`` against the portal's WhatsApp JID
    (e.g. ``66909966651@s.whatsapp.net``). A row with ``contact_jid='*'``
    acts as an explicit fallback that, if present, takes precedence over
    the implicit default in ``agents.whatsapp_login_id``.

    ``priority`` orders rules within the same login: lower wins. The
    ``'*'`` fallback should usually have the highest (largest) priority
    so specific rules win.

    Phase 1 of the WA routing feature is read-only — the rules table
    exists, can be viewed in the UI, but nothing acts on it yet. Phase 3
    will introduce a worker that walks portal rooms and invites the
    matching bot.
    """
    __tablename__ = "wa_routing_rules"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "wa_login_id", "contact_jid",
            name="uq_wa_routing_rule_contact",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # The paired WA login (numeric phone, no ``@s.whatsapp.net`` suffix).
    wa_login_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # The contact's WA JID, or ``'*'`` for fallback. Stored canonical —
    # always lower-cased, including the ``@s.whatsapp.net`` suffix for
    # individual contacts.
    contact_jid: Mapped[str] = mapped_column(String(80), nullable=False)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    Base.metadata.create_all(engine)
    _migrate_add_matrix_columns()
    _migrate_add_user_columns()
    _migrate_decrypt_api_keys()
    _migrate_backfill_owner_members()


def _migrate_decrypt_api_keys():
    """One-shot migration: drop encryption from ``user_api_keys`` and
    ``agent_api_keys``.

    We renamed ``key_encrypted`` → ``api_key`` and switched to plaintext
    (see the class docstrings for rationale). This migration:

      1. If the legacy ``key_encrypted`` column exists, decrypts every row
         and writes the result into a new ``api_key`` column.
      2. Drops the old column.

    Idempotent: skips entirely once the new column is in place.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for tbl in ("user_api_keys", "agent_api_keys"):
        try:
            cols = {c["name"] for c in insp.get_columns(tbl)}
        except Exception:
            continue   # table doesn't exist yet — create_all will make it fresh
        if "key_encrypted" not in cols or "api_key" in cols:
            # Either already migrated, or never had the old column.
            continue

        # Decrypt-in-place: read old rows, decrypt, write into the new column.
        # The ``crypto`` import is local so envs without the lib still boot.
        from .crypto import safe_decrypt

        with engine.begin() as conn:
            # 1. Add the new column (nullable while we backfill).
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN api_key VARCHAR(1024)"))
            # 2. Read + decrypt + write per row.
            rows = conn.execute(text(f"SELECT id, key_encrypted FROM {tbl}")).fetchall()
            for row_id, ciphertext in rows:
                plaintext = safe_decrypt(ciphertext) or ""
                conn.execute(
                    text(f"UPDATE {tbl} SET api_key = :p WHERE id = :id"),
                    {"p": plaintext, "id": row_id},
                )
            # 3. Drop the old column (SQLite ≥ 3.35 supports DROP COLUMN).
            try:
                conn.execute(text(f"ALTER TABLE {tbl} DROP COLUMN key_encrypted"))
            except Exception:
                # Older SQLite: leave it; the ORM will simply ignore it.
                pass


def _migrate_add_user_columns():
    """Add new optional columns to the users table. Idempotent."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("users")}
    additions = [
        ("company_prefix", "VARCHAR(32)"),
    ]
    with engine.begin() as conn:
        for name, typ in additions:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {typ}"))


def _migrate_add_matrix_columns():
    """Lightweight migration: add Matrix columns to an existing agents table.

    Idempotent. Replaces a proper migration tool for v1 — swap for Alembic later.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("agents")}
    additions = [
        ("matrix_user_id", "VARCHAR(255)"),
        ("matrix_access_token", "VARCHAR(500)"),
        ("matrix_device_id", "VARCHAR(120)"),
        ("matrix_account_id", "VARCHAR(80)"),
        ("matrix_password", "VARCHAR(120)"),
        ("whatsapp_login_id", "VARCHAR(120)"),
    ]
    with engine.begin() as conn:
        for name, typ in additions:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE agents ADD COLUMN {name} {typ}"))


def _migrate_backfill_owner_members():
    """Seed `agent_members` with a (owner_user_id, role='owner') row for
    every existing agent that doesn't already have one. Idempotent.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO agent_members (agent_id, user_id, role, created_at)
            SELECT a.id, a.owner_user_id, 'owner', a.created_at
              FROM agents a
             WHERE NOT EXISTS (
                SELECT 1 FROM agent_members m
                 WHERE m.agent_id = a.id AND m.user_id = a.owner_user_id
             )
        """))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
