"""OCPlatform CLI / config-file driven harness."""
from __future__ import annotations
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .base import Harness, AgentSpec, AgentState, MatrixAccount
from ..config import settings


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
LOBSTER_RE = re.compile(r"^🦞 .*$", re.M)


def _strip_banner(s: str) -> str:
    s = ANSI_RE.sub("", s)
    s = LOBSTER_RE.sub("", s)
    return s.strip()


def _try_json(s: str) -> Any:
    s = _strip_banner(s)
    # Find first { or [
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                return json.loads(s[i:])
            except json.JSONDecodeError:
                # try line-by-line
                pass
    return None


class OpenClawError(RuntimeError):
    def __init__(self, msg: str, cmd: list[str], stdout: str, stderr: str, returncode: int):
        super().__init__(msg)
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class OpenClawHarness(Harness):
    name = "openclaw"

    # ---------- low-level CLI runner ----------
    def _run(self, args: list[str], *, input_text: str | None = None, timeout: int = 60) -> str:
        cmd = [settings.oc_cmd, *args]
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise OpenClawError(f"openclaw binary not found: {e}", cmd, "", str(e), 127)
        if proc.returncode != 0:
            raise OpenClawError(
                f"openclaw {' '.join(shlex.quote(a) for a in args)} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}",
                cmd, proc.stdout, proc.stderr, proc.returncode,
            )
        return proc.stdout

    # ---------- config file helpers ----------
    def _read_config(self) -> dict:
        return json.loads(Path(settings.oc_config_path).read_text())

    def _patch_config(self, patch: dict) -> None:
        """Apply a JSON5 patch via `openclaw config patch --stdin`."""
        self._run(["config", "patch", "--stdin"], input_text=json.dumps(patch))

    # ---------- harness API ----------
    def list_models(self) -> list[dict]:
        try:
            cfg = self._read_config()
        except Exception:
            return []
        out: list[dict] = []
        for pid, p in (cfg.get("models", {}).get("providers", {}) or {}).items():
            for m in p.get("models", []) or []:
                mid = m.get("id")
                if not mid:
                    continue
                out.append({
                    "id": f"{pid}/{mid}",
                    "name": m.get("name") or mid,
                    "provider": pid,
                })
        # Also pull aliases not already present
        aliases = cfg.get("agents", {}).get("defaults", {}).get("models", {}) or {}
        seen = {m["id"] for m in out}
        for k, v in aliases.items():
            if k not in seen:
                out.append({"id": k, "name": (v or {}).get("alias") or k, "provider": k.split("/")[0]})
        return out

    def list_channels(self) -> list[str]:
        try:
            cfg = self._read_config()
            return list((cfg.get("channels") or {}).keys())
        except Exception:
            return []

    # --- core CRUD ---
    def create_agent(self, spec: AgentSpec) -> AgentState:
        ws = spec.workspace_path or str(Path(settings.oc_workspaces_root) / spec.harness_agent_id)
        Path(ws).mkdir(parents=True, exist_ok=True)

        # Register channel slots first, then bind the new agent to them.
        bind_targets: list[str] = []
        if spec.telegram_bot_token:
            account_id = spec.telegram_account_id or spec.harness_agent_id
            self._register_telegram_account(account_id, spec.telegram_bot_token)
            bind_targets.append(f"telegram:{account_id}")
        if spec.matrix_account:
            self._register_matrix_account(spec.matrix_account)
            bind_targets.append(f"matrix:{spec.matrix_account.account_id}")

        args = [
            "agents", "add", spec.harness_agent_id,
            "--workspace", ws,
            "--non-interactive",
            "--json",
        ]
        if spec.model:
            args += ["--model", spec.model]
        for b in bind_targets:
            args += ["--bind", b]

        stdout = self._run(args, timeout=120)
        _ = _try_json(stdout)  # not strictly needed

        # Identity (emoji / name)
        if spec.emoji or spec.display_name:
            id_args = ["agents", "set-identity", "--agent", spec.harness_agent_id, "--json"]
            if spec.display_name:
                id_args += ["--name", spec.display_name]
            if spec.emoji:
                id_args += ["--emoji", spec.emoji]
            try:
                self._run(id_args, timeout=30)
            except OCPlatformError:
                pass  # non-fatal

        # System prompt / SOUL.md
        if spec.system_prompt:
            (Path(ws) / "SOUL.md").write_text(spec.system_prompt)

        return self.get_agent_state(spec.harness_agent_id)

    def update_agent(self, spec: AgentSpec) -> AgentState:
        # Identity updates via CLI
        if spec.display_name or spec.emoji:
            id_args = ["agents", "set-identity", "--agent", spec.harness_agent_id, "--json"]
            if spec.display_name:
                id_args += ["--name", spec.display_name]
            if spec.emoji:
                id_args += ["--emoji", spec.emoji]
            self._run(id_args, timeout=30)

        # Model update: patch config.agents.list[].model.primary
        if spec.model:
            cfg = self._read_config()
            agents_list = (cfg.get("agents") or {}).get("list") or []
            for i, a in enumerate(agents_list):
                if a.get("id") == spec.harness_agent_id:
                    agents_list[i] = {**a, "model": {"primary": spec.model}}
                    break
            self._patch_config({"agents": {"list": agents_list}})

        # System prompt / SOUL.md
        if spec.system_prompt is not None and spec.workspace_path:
            (Path(spec.workspace_path) / "SOUL.md").write_text(spec.system_prompt)

        # Telegram token replacement
        if spec.telegram_bot_token is not None:
            account_id = spec.telegram_account_id or spec.harness_agent_id
            self._register_telegram_account(account_id, spec.telegram_bot_token)
            # Ensure binding
            try:
                self._run(["agents", "bind", "--agent", spec.harness_agent_id,
                           "--bind", f"telegram:{account_id}", "--json"], timeout=30)
            except OpenClawError:
                pass

        # Matrix account refresh / re-bind
        if spec.matrix_account is not None:
            self._register_matrix_account(spec.matrix_account)
            try:
                self._run(["agents", "bind", "--agent", spec.harness_agent_id,
                           "--bind", f"matrix:{spec.matrix_account.account_id}", "--json"], timeout=30)
            except OpenClawError:
                pass

        return self.get_agent_state(spec.harness_agent_id)

    def delete_agent(self, harness_agent_id: str) -> None:
        try:
            self._run(["agents", "delete", harness_agent_id, "--force", "--json"], timeout=60)
        except OpenClawError as e:
            # If agent is already gone in config but state lingers, swallow "not found".
            if "not found" in (e.stderr + e.stdout).lower():
                return
            raise

    def get_agent_state(self, harness_agent_id: str) -> AgentState:
        try:
            stdout = self._run(["agents", "list", "--bindings", "--json"], timeout=30)
        except OpenClawError:
            return AgentState(exists=False, harness_agent_id=harness_agent_id)
        data = _try_json(stdout) or {}
        # Normalize: data may be {agents:[...]}, {list:[...]}, or plain list
        agents = data.get("agents") if isinstance(data, dict) else None
        if agents is None and isinstance(data, dict):
            agents = data.get("list")
        if agents is None and isinstance(data, list):
            agents = data
        agents = agents or []
        for a in agents:
            if a.get("id") == harness_agent_id:
                return AgentState(
                    exists=True,
                    harness_agent_id=harness_agent_id,
                    workspace_path=a.get("workspace"),
                    model=(a.get("model") or {}).get("primary") if isinstance(a.get("model"), dict) else a.get("model"),
                    bindings=a.get("bindings") or [],
                    raw=a,
                )
        return AgentState(exists=False, harness_agent_id=harness_agent_id)

    # --- channel-specific helpers ---
    def _register_telegram_account(self, account_id: str, bot_token: str) -> None:
        """Patch openclaw.json to register a Telegram bot account slot.

        OpenClaw supports two shapes:
          (a) legacy single-bot:  channels.telegram.botToken
          (b) multi-account:      channels.telegram.accounts.<id>.botToken

        As soon as ANY entry exists under `accounts.*`, OCPlatform stops loading
        the legacy top-level botToken. That used to silently break the existing
        default bot the first time a user added a new one. To avoid that, we
        migrate the legacy bot into `accounts.default` (preserving its bindings)
        in the same patch where we add the new account.
        """
        accounts: dict[str, dict] = {
            account_id: {
                "botToken": bot_token,
                "enabled": True,
                "dmPolicy": "open",
                "allowFrom": ["*"],
            }
        }

        # If there's a legacy single-bot config and no `accounts.default` yet,
        # migrate it so adding new accounts doesn't kill the default bot.
        try:
            cfg = self._read_config()
            tg = (cfg.get("channels") or {}).get("telegram") or {}
            existing_accounts = tg.get("accounts") or {}
            legacy_token = tg.get("botToken")
            if legacy_token and "default" not in existing_accounts and account_id != "default":
                accounts["default"] = {
                    "botToken": legacy_token,
                    "enabled": tg.get("enabled", True),
                    "dmPolicy": tg.get("dmPolicy", "open"),
                    "allowFrom": tg.get("allowFrom", ["*"]),
                }
        except Exception:
            # Best-effort migration; if config read fails we just patch the new account.
            pass

        patch = {
            "channels": {
                "telegram": {
                    "enabled": True,
                    "accounts": accounts,
                }
            }
        }
        self._patch_config(patch)

    def _register_matrix_account(self, acct: MatrixAccount) -> None:
        """Patch openclaw.json to register a Matrix bot account.

        Also forces open DM policy + auto-join so anyone can DM any bot
        without manual `openclaw pairing approve` ceremony. These keys are
        channel-level (not per-account), so this is idempotent across bots.
        """
        patch = {
            "channels": {
                "matrix": {
                    "enabled": True,
                    "allowlistOnly": False,
                    "autoJoin": "always",
                    "dm": {"policy": "open"},
                    # Allow group/room messages by default. Without this,
                    # `resolveAllowlistProviderRuntimeGroupPolicy` defaults
                    # to `"allowlist"` (because the matrix provider IS
                    # configured) and every room message is silently
                    # dropped at `matrix: drop room message (no allowlist
                    # ...)`. Mautrix WhatsApp portals look like 5-member
                    # rooms (bot + web user + bridge bot + 2 ghosts), so
                    # OpenClaw's strict 2-member DM check rejects them
                    # and they fall through this group path.
                    "groupPolicy": "open",
                    "accounts": {
                        acct.account_id: {
                            "homeserver": acct.homeserver,
                            "userId": acct.user_id,
                            "accessToken": acct.access_token,
                            "deviceName": acct.device_name or "agent-admin",
                            "enabled": True,
                            # Allow any sender to DM this bot. Channel-level
                            # `dm.policy: open` is not enough — OpenClaw
                            # only consults `accounts.<id>.dm.allowFrom` (or
                            # the channel-level `matrix.dm.allowFrom`) for
                            # the DM allowlist. A bare
                            # `accounts.<id>.allowFrom` is silently ignored
                            # and every DM gets dropped with the log line
                            # `matrix: blocked dm sender ... (matchKey=none
                            # matchSource=none)`. See
                            # `resolveMatrixAccountAllowlistConfig` in
                            # openclaw's `account-config-*.js`.
                            "dm": {"allowFrom": ["*"]},
                            # Mirror channel-level groupPolicy so it works
                            # even if a future migration drops the global.
                            "groupPolicy": "open",
                            # In rooms (groups), default behaviour is
                            # `requireMention=true` — the bot only responds
                            # when @-mentioned. WA portals contain ghosts
                            # that can't @-mention the bot, so the bot would
                            # never reply. `rooms["*"].autoReply=true`
                            # tells the bot to reply to every inbound room
                            # message regardless of mention. (Bridge relay
                            # mode already guarantees only the paired WA
                            # user can route messages to the portal, so
                            # this is safe.)
                            "rooms": {"*": {"autoReply": True}},
                            # For group/channel chats, OpenClaw's default
                            # `sourceReplyDeliveryMode` is
                            # `message_tool_only` — agent must explicitly
                            # call the `message` tool to send a reply. We
                            # want the assistant's plain text response to
                            # land in the channel automatically, same as a
                            # DM. See `resolveSourceReplyDeliveryMode` in
                            # `source-reply-delivery-mode-*.js`.
                            "messages": {
                                "groupChat": {"visibleReplies": "automatic"}
                            },
                        }
                    },
                }
            }
        }
        self._patch_config(patch)
