"""FastAPI entrypoint."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .db import init_db
from .routes_auth import router as auth_router
from .routes_agents import router as agents_router
from .routes_members import router as members_router
from .routes_agent_keys import router as agent_keys_router
from .routes_rooms import router as rooms_router
from .routes_skills import router as skills_router
from .routes_me import router as me_router
from .routes_custom_providers import router as custom_providers_router
from .routes_whatsapp import router as whatsapp_router
from .routes_agent_whatsapp import router as agent_whatsapp_router
from .routes_wa_routing import router as wa_routing_router
from .whatsapp_client import WhatsAppBridge, WhatsAppBridgeConfig


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # mautrix-whatsapp bridge (optional). Initialised only if both URL
    # and shared_secret are configured; otherwise the whatsapp routes
    # return 503 and the frontend hides the WA panel.
    bridge_cfg = WhatsAppBridgeConfig(
        base_url=settings.whatsapp_bridge_url,
        shared_secret=settings.whatsapp_shared_secret,
    )
    if bridge_cfg.configured:
        app.state.whatsapp_bridge = WhatsAppBridge(bridge_cfg)
    else:
        app.state.whatsapp_bridge = None

    try:
        yield
    finally:
        if app.state.whatsapp_bridge is not None:
            await app.state.whatsapp_bridge.aclose()


app = FastAPI(title="Agent Admin", version="0.1.0", lifespan=lifespan)

# CORS — only needed in dev (Vite on a different port).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5190",
        "http://127.0.0.1:5190",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(members_router)
app.include_router(agent_keys_router)
app.include_router(rooms_router)
app.include_router(skills_router)
app.include_router(me_router)
app.include_router(custom_providers_router)
app.include_router(whatsapp_router)
app.include_router(agent_whatsapp_router)
app.include_router(wa_routing_router)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "agent-admin"}


# Validation errors → 400
@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"detail": exc.errors()},
    )


# Serve frontend (production build) if present.
FRONTEND_DIST: Path = settings.frontend_dist
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # Let API routes 404 through their own handlers; this only catches non-/api paths.
        target = FRONTEND_DIST / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "frontend not built"}, status_code=404)
