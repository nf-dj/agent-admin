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

    @classmethod
    def from_agent(cls, a, my_role: str = "owner"):
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

