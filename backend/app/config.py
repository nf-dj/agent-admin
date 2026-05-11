"""Settings, loaded from env (with .env support)."""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ADMIN_", extra="ignore")

    # HTTP
    host: str = "127.0.0.1"
    port: int = 5191

    # Security
    secret_key: str = "change-me-please-use-env"
    cookie_name: str = "agent_admin_session"
    session_max_age_seconds: int = 12 * 3600  # 12h
    secure_cookies: bool = False  # set true behind HTTPS proxy

    # Database
    db_path: Path = Path(__file__).resolve().parent.parent / "agent_admin.sqlite"

    # OpenClaw integration
    oc_cmd: str = "openclaw"
    oc_config_path: Path = Path.home() / ".openclaw" / "openclaw.json"
    oc_workspaces_root: Path = Path.home() / ".openclaw" / "user-workspaces"

    # Signup policy
    allow_signup: bool = True

    # Matrix integration (optional). If MATRIX_HOMESERVER + admin creds are
    # configured, a Matrix user is auto-provisioned for each new agent.
    matrix_enabled: bool = False
    matrix_homeserver: str = ""           # e.g. https://matrix.netforce.com
    matrix_server_name: str = ""          # e.g. matrix.netforce.com (the part after the colon in @user:server)
    matrix_admin_user: str = ""           # e.g. admin
    matrix_admin_password: str = ""
    matrix_user_prefix: str = "bot_"      # username prefix for bot users

    # Frontend dist (if backend should serve it)
    frontend_dist: Path = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


settings = Settings()
