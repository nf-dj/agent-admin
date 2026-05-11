"""Base classes for the harness abstraction."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MatrixAccount:
    """Inputs for registering a Matrix account in the harness config."""
    account_id: str   # slot key under channels.matrix.accounts
    homeserver: str   # e.g. https://matrix.netforce.com
    user_id: str      # full mxid, e.g. @bot_xxx:matrix.netforce.com
    access_token: str
    device_name: str | None = None


@dataclass
class AgentSpec:
    """Inputs for creating/updating an agent on a harness."""
    harness_agent_id: str
    display_name: str
    model: str | None = None
    emoji: str | None = None
    system_prompt: str | None = None
    workspace_path: str | None = None
    telegram_bot_token: str | None = None
    telegram_account_id: str | None = None  # slot key in channels.telegram.accounts
    matrix_account: "MatrixAccount | None" = None  # populated by sync layer


@dataclass
class AgentState:
    """Runtime state of an agent as the harness sees it."""
    exists: bool
    harness_agent_id: str
    workspace_path: str | None = None
    model: str | None = None
    bindings: list[dict] = field(default_factory=list)
    raw: dict | None = None


class Harness(ABC):
    name: str = "base"

    @abstractmethod
    def create_agent(self, spec: AgentSpec) -> AgentState: ...

    @abstractmethod
    def update_agent(self, spec: AgentSpec) -> AgentState: ...

    @abstractmethod
    def delete_agent(self, harness_agent_id: str) -> None: ...

    @abstractmethod
    def get_agent_state(self, harness_agent_id: str) -> AgentState: ...

    @abstractmethod
    def list_models(self) -> list[dict]: ...
