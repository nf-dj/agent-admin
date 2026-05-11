"""Pydantic schemas for API request/response bodies."""
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator


# --- Auth ---
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    is_admin: bool
    created_at: datetime
    company_prefix: str | None = None

    @classmethod
    def from_user(cls, u):
        return cls(
            id=u.id, email=u.email, display_name=u.display_name,
            is_admin=bool(u.is_admin), created_at=u.created_at,
            company_prefix=getattr(u, "company_prefix", None),
        )


# --- User API keys (per-user defaults for new agents) ---
# Provider IDs match those used in ``openclaw.json``. ``anthropic-subscription``
# is a synthetic id for Claude OAuth/subscription tokens (different field).
ALLOWED_KEY_PROVIDERS = {
    "deepseek", "openai", "openrouter", "groq", "mistral",
    "anthropic", "anthropic-subscription",
}


class UserApiKeyOut(BaseModel):
    """Public view of a stored key — never includes the plaintext."""
    provider: str
    has_key: bool
    preview: str | None = None
    updated_at: datetime | None = None


class UserApiKeySet(BaseModel):
    """Body for PUT /api/me/api-keys/{provider}. Empty/whitespace = error;
    use DELETE to clear."""
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("api_key must not be blank")
        return v


class AgentApiKeyOut(BaseModel):
    """Per-agent key view. Includes whether the user has a saved fallback
    so the UI can show a meaningful 'effective source' label.
    """
    provider: str
    has_override: bool
    override_preview: str | None = None
    override_updated_at: datetime | None = None
    user_has_saved: bool = False
    user_saved_preview: str | None = None


class AgentSkillOut(BaseModel):
    """Summary of one skill installed in an agent's workspace.

    Parsed from the YAML frontmatter of ``SKILL.md``. Fields are nullable
    because skill manifests in the wild aren't perfectly uniform — we
    surface what we find and let the UI cope with gaps.
    """
    name: str
    description: str | None = None
    version: str | None = None
    author: str | None = None
    updated: str | None = None
    # Relative path from workspace root to the skill's folder. Useful for
    # the detail view and for power-users who want to ssh in and edit.
    path: str


class AgentSkillDetailOut(AgentSkillOut):
    """Skill summary + full SKILL.md content (markdown source)."""
    content: str


class AgentRoomMessageOut(BaseModel):
    """One Matrix message event for the owner-view timeline.

    Only ``m.room.message`` events are surfaced (no joins/state/reactions).
    ``is_bot`` lets the UI right-align the bot's own messages and style
    them differently — a familiar 'me' vs 'them' chat layout.
    """
    event_id: str
    sender: str            # full matrix id, e.g. @alice:matrix.netforce.com
    sender_name: str       # display name or short id fallback
    body: str              # plain-text content (no markdown / HTML render)
    msgtype: str           # m.text | m.notice | m.image | m.file | ...
    ts: int                # origin_server_ts (epoch ms)
    is_bot: bool           # True when sender == this agent's matrix_user_id


class AgentRoomMessagesOut(BaseModel):
    """Paginated response from /rooms/{id}/messages.

    ``messages`` is ordered oldest-first (natural reading). ``prev_token``
    is the cursor to fetch *older* messages on a subsequent request (pass
    as the ``from`` query param). ``has_more`` is a UI hint — false means
    'no point showing a Load older button'.
    """
    messages: list[AgentRoomMessageOut]
    prev_token: str | None = None
    has_more: bool = False


class AgentRoomSendBody(BaseModel):
    """Body for POST /rooms/{id}/send. Plain text only for v1.

    ``text`` is stripped before validation so leading/trailing whitespace
    doesn't pad the message. After strip we re-check the length so a
    whitespace-only payload is rejected as too short rather than falling
    through into Matrix.
    """
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text", mode="before")
    @classmethod
    def _strip(cls, v):
        # Run early so length-validation sees the stripped value. Returning
        # the stripped string lets ``min_length=1`` reject whitespace-only
        # input with the standard 422 response (no custom error handler
        # needed, no ValueError-in-JSON serialization quirks).
        if isinstance(v, str):
            return v.strip()
        return v


class AgentRoomSendResult(BaseModel):
    """Response from a successful send. Includes the new event id so the
    UI can optimistically attach the new message to the timeline."""
    event_id: str
    sent_at: int           # client-side timestamp, epoch ms


class AgentRoomOut(BaseModel):
    """One Matrix room the bot is currently joined to.

    ``is_dm`` is True when the room has exactly two joined members — a
    convention Matrix clients use to render "direct message" UIs. For DMs
    we surface the other party's user id so the owner can see who they're
    talking to without opening the room.
    """
    room_id: str
    name: str
    member_count: int
    is_dm: bool
    other_user_id: str | None = None


class AgentApiKeySet(BaseModel):
    """Body for PUT /api/agents/{id}/api-keys/{provider}."""
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("api_key must not be blank")
        return v


# --- User settings (PATCH /api/me/settings) ---
class UserSettingsUpdate(BaseModel):
    """All fields optional; only sent ones are updated."""
    display_name: str | None = Field(default=None, max_length=120)
    # Empty string → clear the prefix (fall back to ``u<id>-``).
    company_prefix: str | None = Field(default=None, max_length=32)

    @field_validator("company_prefix")
    @classmethod
    def _validate_prefix(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return ""   # signals "clear it"
        import re
        # Lowercase alnum + single hyphens; no leading/trailing hyphens.
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", v):
            raise ValueError(
                "Prefix must be lowercase letters, digits, and hyphens only "
                "(no leading/trailing/double hyphens)."
            )
        if len(v) > 32:
            raise ValueError("Prefix must be 32 characters or fewer.")
        return v


# --- Agents ---
class AgentCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=60)  # optional ID hint
    model: str | None = None
    emoji: str | None = Field(default=None, max_length=20)
    system_prompt: str | None = Field(default=None, max_length=20000)
    telegram_bot_token: str | None = Field(default=None, max_length=255)
    harness: str = Field(default="openclaw")
    # Provider credentials. Accepted on create; persistence into the
    # openclaw config is handled by the harness adapter (TODO).
    provider_api_key: str | None = Field(default=None, max_length=512)
    claude_subscription_key: str | None = Field(default=None, max_length=2048)

    @field_validator("slug")
    @classmethod
    def slug_chars(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("slug must be lowercase letters/digits/_/-, starting with alnum")
        return v


class AgentUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    model: str | None = None
    emoji: str | None = Field(default=None, max_length=20)
    system_prompt: str | None = Field(default=None, max_length=20000)
    telegram_bot_token: str | None = Field(default=None, max_length=255)


class AgentOut(BaseModel):
    id: int
    harness: str
    harness_agent_id: str
    display_name: str
    model: str | None
    emoji: str | None
    has_telegram: bool
    matrix_user_id: str | None
    workspace_path: str | None
    created_at: datetime
    updated_at: datetime
    # Role of the current user on this agent. "owner" or "member".
    my_role: str = "owner"
    # Number of Matrix rooms this bot is currently joined to. None when
    # the bot has no Matrix account, Matrix is unavailable, or the lookup
    # failed — the dashboard treats null as "don't render the badge".
    room_count: int | None = None
    # Number of skills found in the agent's workspace. Same null-means-hidden
    # convention as ``room_count``. Owner-only — members see None.
    skill_count: int | None = None

    @classmethod
    def from_agent(cls, a, my_role: str = "owner", room_count: int | None = None,
                   skill_count: int | None = None):
        return cls(
            id=a.id,
            harness=a.harness,
            harness_agent_id=a.harness_agent_id,
            display_name=a.display_name,
            model=a.model,
            emoji=a.emoji,
            has_telegram=bool(a.telegram_bot_token),
            matrix_user_id=a.matrix_user_id,
            workspace_path=a.workspace_path,
            created_at=a.created_at,
            updated_at=a.updated_at,
            my_role=my_role,
            room_count=room_count,
            skill_count=skill_count,
        )


class AgentDetailOut(AgentOut):
    system_prompt: str | None = None
    runtime: dict | None = None  # state from harness

    @classmethod
    def from_agent_with_runtime(cls, a, runtime: dict | None, my_role: str = "owner"):
        base = AgentOut.from_agent(a, my_role=my_role).model_dump()
        return cls(**base, system_prompt=a.system_prompt, runtime=runtime)


class AgentChatInfoOut(BaseModel):
    """Slim view exposed to non-owner members — just enough to start a DM."""
    id: int
    display_name: str
    emoji: str | None
    matrix_user_id: str | None
    my_role: str


# --- Members ---
class AgentMemberOut(BaseModel):
    user_id: int
    email: str
    display_name: str | None
    role: str
    created_at: datetime


class AgentMemberInvite(BaseModel):
    email: EmailStr


class ModelOut(BaseModel):
    id: str
    name: str
    provider: str


class HarnessOut(BaseModel):
    name: str
    display_name: str
    available: bool

class WebMatrixCredsOut(BaseModel):
    """Credentials handed to the browser to drive matrix-js-sdk."""
    matrix_user_id: str
    homeserver: str
    access_token: str
    device_id: str

