"""User-owned custom LLM providers (BYO endpoints).

CRUD over ``custom_providers``. Every mutation re-syncs the user's block in
``openclaw.json`` via :mod:`auth_sync` so the harness picks it up on its
next request.

Each user gets their own namespaced provider id (``u<userId>-<slug>``) so
two users can register providers with the same short slug without
colliding on disk.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from .auth_sync import (
    namespaced_provider_id, sync_custom_provider, remove_custom_provider,
)
from .crypto import make_preview
from .db import CustomProvider, User, get_db
from .schemas import (
    ALLOWED_CUSTOM_API_TYPES,
    CustomModelDef, CustomProviderCreate, CustomProviderOut, CustomProviderUpdate,
)

log = logging.getLogger(__name__)


router = APIRouter(prefix="/api/me/providers", tags=["custom-providers"])


def _to_out(row: CustomProvider) -> CustomProviderOut:
    try:
        models_raw = json.loads(row.models_json or "[]")
    except json.JSONDecodeError:
        models_raw = []
    models = [CustomModelDef(**m) for m in models_raw if isinstance(m, dict)]
    return CustomProviderOut(
        id=row.id,
        slug=row.slug,
        display_name=row.display_name,
        base_url=row.base_url,
        api_type=row.api_type,
        has_api_key=bool(row.api_key),
        api_key_preview=make_preview(row.api_key) if row.api_key else None,
        models=models,
        namespaced_id=namespaced_provider_id(row.user_id, row.slug),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _models_to_json(models: list[CustomModelDef]) -> str:
    # ``exclude_none`` drops optional fields like ``compat`` when unset,
    # so we never write ``"compat": null`` into openclaw.json (which
    # rejects nulls and would fail validation on the next config patch).
    return json.dumps([m.model_dump(exclude_none=True) for m in models])


@router.get("", response_model=list[CustomProviderOut])
def list_custom_providers(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(CustomProvider)
        .filter(CustomProvider.user_id == current.id)
        .order_by(CustomProvider.created_at.desc())
        .all()
    )
    return [_to_out(r) for r in rows]


@router.post("", response_model=CustomProviderOut, status_code=201)
def create_custom_provider(
    body: CustomProviderCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(CustomProvider)
        .filter(
            CustomProvider.user_id == current.id,
            CustomProvider.slug == body.slug,
        )
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"You already have a provider with slug {body.slug!r}",
        )

    row = CustomProvider(
        user_id=current.id,
        slug=body.slug,
        display_name=body.display_name,
        base_url=body.base_url,
        api_type=body.api_type,
        api_key=body.api_key,
        models_json=_models_to_json(body.models),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    sync_custom_provider(row)
    return _to_out(row)


def _get_owned(db: Session, user: User, provider_id: int) -> CustomProvider:
    row = (
        db.query(CustomProvider)
        .filter(
            CustomProvider.id == provider_id,
            CustomProvider.user_id == user.id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return row


@router.get("/{provider_id}", response_model=CustomProviderOut)
def get_custom_provider(
    provider_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _to_out(_get_owned(db, current, provider_id))


@router.patch("/{provider_id}", response_model=CustomProviderOut)
def update_custom_provider(
    provider_id: int,
    body: CustomProviderUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _get_owned(db, current, provider_id)

    if body.display_name is not None:
        row.display_name = body.display_name
    if body.base_url is not None:
        row.base_url = body.base_url
    if body.api_type is not None:
        row.api_type = body.api_type
    if body.clear_api_key:
        row.api_key = None
    elif body.api_key is not None:
        # Only overwrite if explicitly provided.
        stripped = body.api_key.strip()
        row.api_key = stripped or None
    if body.models is not None:
        row.models_json = _models_to_json(body.models)

    db.commit()
    db.refresh(row)

    sync_custom_provider(row)
    return _to_out(row)


@router.delete("/{provider_id}", status_code=204)
def delete_custom_provider(
    provider_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _get_owned(db, current, provider_id)
    slug = row.slug
    user_id = row.user_id

    db.delete(row)
    db.commit()

    remove_custom_provider(user_id, slug)
    return None


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------
#
# Two flavours:
#   POST /api/me/providers/{id}/test       — test a saved provider as-is
#   POST /api/me/providers/test            — test an unsaved payload (form preview)
#
# We hit a cheap, well-known endpoint per ``api_type`` to confirm the
# server is reachable AND the credentials (if any) are accepted. Network
# errors and HTTP errors are translated into a structured ``TestResult``
# rather than raising — the UI wants to render a red/green badge.


class TestResult(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    # When the probe call returns a model list, surface it so users can
    # see what their endpoint is actually exposing.
    discovered_models: list[str] | None = None
    error: str | None = None
    endpoint: str | None = None  # the URL we actually probed


class TestProviderPayload(BaseModel):
    """Unsaved-form variant of :class:`CustomProviderCreate` for live testing.

    Same shape but every field is optional so the UI can ping with just
    ``base_url`` + ``api_type`` while the user is still filling things in.
    """
    base_url: str
    api_type: str = "openai-completions"
    api_key: str | None = None


def _probe(base_url: str, api_type: str, api_key: str | None) -> TestResult:
    base_url = base_url.rstrip("/")
    if api_type not in ALLOWED_CUSTOM_API_TYPES:
        return TestResult(ok=False, error=f"unknown api_type {api_type!r}")

    headers: dict[str, str] = {"accept": "application/json"}
    url: str
    parse_models: bool = True

    if api_type in ("openai-completions", "openai-chat"):
        url = f"{base_url}/v1/models"
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
    elif api_type == "anthropic":
        url = f"{base_url}/v1/models"
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif api_type == "google":
        # Gemini API uses ?key=...
        qs = urllib.parse.urlencode({"key": api_key}) if api_key else ""
        url = f"{base_url}/v1beta/models" + (f"?{qs}" if qs else "")
    elif api_type == "ollama":
        url = f"{base_url}/api/tags"
    else:
        # Defensive — ALLOWED_CUSTOM_API_TYPES check above should catch this.
        return TestResult(ok=False, error=f"unsupported api_type {api_type!r}")

    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        # 8s is enough for a healthy local server, short enough that a
        # wrong URL fails fast.
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=8.0, context=ctx) as resp:
            latency = int((time.monotonic() - started) * 1000)
            body = resp.read(64 * 1024)  # cap response read
            status = resp.status
            discovered = _extract_model_ids(body, api_type) if parse_models else None
            return TestResult(
                ok=True, status_code=status, latency_ms=latency,
                discovered_models=discovered, endpoint=url,
            )
    except urllib.error.HTTPError as e:
        latency = int((time.monotonic() - started) * 1000)
        body_snippet = ""
        try:
            body_snippet = e.read(512).decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        # Map common cases to friendlier errors.
        if e.code in (401, 403):
            msg = f"auth rejected ({e.code}): {body_snippet[:200] or e.reason}"
        elif e.code == 404:
            msg = (f"endpoint not found ({url}). Is the server an "
                   f"{api_type!r} server? Got: {body_snippet[:200] or e.reason}")
        else:
            msg = f"HTTP {e.code}: {body_snippet[:200] or e.reason}"
        return TestResult(
            ok=False, status_code=e.code, latency_ms=latency,
            error=msg, endpoint=url,
        )
    except urllib.error.URLError as e:
        latency = int((time.monotonic() - started) * 1000)
        reason = getattr(e, "reason", e)
        return TestResult(
            ok=False, latency_ms=latency,
            error=f"connection failed: {reason}", endpoint=url,
        )
    except socket.timeout:
        return TestResult(ok=False, error="connection timed out (8s)", endpoint=url)
    except Exception as e:  # belt-and-suspenders
        log.exception("Unexpected error probing %s", url)
        return TestResult(ok=False, error=f"{type(e).__name__}: {e}", endpoint=url)


def _extract_model_ids(body: bytes, api_type: str) -> list[str] | None:
    """Best-effort extraction of model ids from a probe response.

    Different APIs use different shapes; we try the common ones and bail
    quietly if nothing matches.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    candidates: list = []
    if isinstance(data, dict):
        # OpenAI / Anthropic shape: {data: [{id: ...}, ...]}
        if isinstance(data.get("data"), list):
            candidates = data["data"]
        # Google: {models: [{name: "models/gemini-..."}, ...]}
        elif isinstance(data.get("models"), list):
            candidates = data["models"]
        # Ollama: {models: [{name: ...}]} (same key, slightly different shape)
    elif isinstance(data, list):
        candidates = data

    out: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            for key in ("id", "name", "model"):
                v = item.get(key)
                if isinstance(v, str) and v:
                    out.append(v)
                    break
        elif isinstance(item, str):
            out.append(item)
    # Cap so we don't blow up the response.
    return out[:50] if out else None


@router.post("/{provider_id}/test", response_model=TestResult)
def test_saved_provider(
    provider_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Probe an already-saved provider with its stored credentials."""
    row = _get_owned(db, current, provider_id)
    return _probe(row.base_url, row.api_type, row.api_key)


@router.post("/test", response_model=TestResult)
def test_unsaved_provider(
    body: TestProviderPayload,
    current: User = Depends(get_current_user),
):
    """Probe a not-yet-saved payload — useful while the user is filling the form.

    Validates URL shape and ``api_type`` lightly here so a typo in the
    form yields a useful error rather than a 422.
    """
    url = (body.base_url or "").strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        return TestResult(ok=False, error="base_url must start with http:// or https://")
    api_type = (body.api_type or "openai-completions").strip().lower()
    if api_type not in ALLOWED_CUSTOM_API_TYPES:
        return TestResult(
            ok=False,
            error=f"api_type must be one of {sorted(ALLOWED_CUSTOM_API_TYPES)}",
        )
    api_key = (body.api_key or "").strip() or None
    return _probe(url, api_type, api_key)
