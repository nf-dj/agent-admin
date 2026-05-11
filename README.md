# Agent Admin

A web app for creating and managing OpenClaw agents.
Lives at https://bots.netforce.com (once Caddy is configured).

## Stack
- **Backend:** Python 3.12 + FastAPI + SQLAlchemy + SQLite + bcrypt
- **Frontend:** Vite + React + TypeScript
- **Reverse proxy:** Caddy → `localhost:5191`
- **Process supervisor:** systemd

## What it does
- Multi-user signup / login (bcrypt + signed session cookies).
- Each user sees only their own agents.
- Create / edit / delete agents:
  - Display name, emoji, model, system prompt (→ SOUL.md).
  - Optional Telegram bot token; if supplied, the agent is bound to that bot.
- Pluggable "harness" abstraction so we can add Hermes etc. later.
  v1 only supports `openclaw`.

## Layout
```
agent-admin/
├── backend/                  FastAPI app
│   ├── app/
│   │   ├── main.py           entrypoint
│   │   ├── config.py         env-driven settings
│   │   ├── db.py             SQLAlchemy models (User, Agent)
│   │   ├── auth.py           bcrypt + itsdangerous session cookies
│   │   ├── schemas.py        Pydantic schemas
│   │   ├── routes_auth.py    /api/auth/{signup,login,logout,me}
│   │   ├── routes_agents.py  /api/agents, /api/models, /api/harnesses
│   │   └── harness/          harness adapters
│   │       ├── base.py       Harness ABC + AgentSpec/State
│   │       └── openclaw.py shells out to `openclaw` CLI
│   ├── requirements.txt
│   └── .env                  ADMIN_SECRET_KEY=…, etc.
├── frontend/                 Vite + React + TS app
│   └── src/
│       ├── api.ts            fetch wrapper
│       ├── App.tsx           top-level routing/state
│       └── components/
│           ├── AuthScreen.tsx
│           ├── Dashboard.tsx
│           ├── CreateAgentView.tsx
│           └── AgentDetailView.tsx
└── deploy/
    ├── agent-admin.service   systemd unit
    ├── caddy-snippet.txt     Caddyfile block
    └── install.sh            one-shot installer (needs sudo)
```

## Local dev

```bash
# Backend (port 5191)
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 5191

# Frontend (port 5190, proxies /api → 5191)
cd ../frontend
npm install
npm run dev
```

Open http://localhost:5190 — first signup gets `is_admin = true`.

## Production deploy

Run `deploy/install.sh` from an account with sudo. It will:

1. Install `/etc/systemd/system/agent-admin.service`.
2. Generate `backend/.env` with a random `ADMIN_SECRET_KEY` (if missing).
3. Build the frontend.
4. Add a `bots.netforce.com` block to `/etc/caddy/Caddyfile` and reload Caddy.
5. Start and enable the systemd service.

## Environment variables (`backend/.env`)
| Variable | Default | What |
|---|---|---|
| `ADMIN_SECRET_KEY` | required | Used to sign session cookies. Long random string. |
| `ADMIN_PORT` | `5191` | Local port the backend listens on. |
| `ADMIN_SECURE_COOKIES` | `false` | Set `true` behind HTTPS. |
| `ADMIN_ALLOW_SIGNUP` | `true` | Set `false` to lock signup. |
| `ADMIN_OC_CMD` | `openclaw` | Path to openclaw CLI. |
| `ADMIN_OC_CONFIG_PATH` | `~/.ocplatform/openclaw.json` | Active config to patch. |
| `ADMIN_OC_WORKSPACES_ROOT` | `~/.openclaw/user-workspaces` | Where per-user agent workspaces live. |

## Adding a new harness
1. Create `backend/app/harness/<name>.py` subclassing `Harness`.
2. Register it in `backend/app/harness/__init__.py`'s `HARNESSES` dict.
3. The UI picks it up automatically via `/api/harnesses`.

## Notes / TODO
- Telegram bot tokens are stored plaintext in SQLite for v1. Wrap with Fernet before going wider.
- No password reset flow yet.
- No quota / rate limits.
- Workspace markdown editor (`AGENTS.md` etc.) was scoped out for v1, only `SOUL.md` is editable via the system prompt field.
