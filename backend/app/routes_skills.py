"""List the skills installed in an agent's workspace.

Owner-only. Reads ``<workspace>/skills/<name>/SKILL.md`` (the OpenClaw
skill manifest convention) and returns parsed summaries.

Each SKILL.md uses YAML frontmatter \u2014 a ``---``-delimited block at the
top of the file \u2014 with at least ``name`` and ``description``. Example::

    ---
    name: bank-recon
    description: >
      Perform bank reconciliation on Netforce ERP via MCP endpoint.
    metadata:
      author: alwin
      version: "2.0"
      updated: 2026-05-11
    ---

    # Bank Reconciliation
    ...

We tolerate two on-disk layouts:

* ``<workspace>/skills/<name>/SKILL.md`` \u2014 the canonical OpenClaw layout
* ``<workspace>/<name>/SKILL.md``         \u2014 also seen in the wild

Path traversal is blocked by resolving each candidate against the agent's
workspace root and rejecting anything that escapes it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import yaml

from .auth import get_current_user
from .db import User, get_db
from .permissions import require_owner
from .schemas import AgentSkillOut, AgentSkillDetailOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-skills"])


# How many bytes of SKILL.md we'll parse for the summary view. Plenty for
# the frontmatter; full content is only loaded via the detail endpoint.
_SKILL_MAX_BYTES = 256 * 1024


@router.get("/{agent_id}/skills", response_model=list[AgentSkillOut])
def list_agent_skills(agent_id: int,
                      current: User = Depends(get_current_user),
                      db: Session = Depends(get_db)) -> list[AgentSkillOut]:
    """Return a summary of every skill in this agent's workspace.

    Sorted alphabetically by skill name.
    """
    agent = require_owner(db, current, agent_id)
    workspace = _resolve_workspace(agent)
    if workspace is None or not workspace.exists():
        return []

    return [_summarize(p, workspace) for p in _iter_skill_manifests(workspace)]


@router.get("/{agent_id}/skills/{skill_name}", response_model=AgentSkillDetailOut)
def get_agent_skill(agent_id: int, skill_name: str,
                    current: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> AgentSkillDetailOut:
    """Return the full SKILL.md content + parsed metadata for one skill."""
    agent = require_owner(db, current, agent_id)
    workspace = _resolve_workspace(agent)
    if workspace is None or not workspace.exists():
        raise HTTPException(status_code=404, detail="agent has no workspace")

    # Find the matching SKILL.md (skill_name comes from the URL \u2014 don't trust it).
    for manifest in _iter_skill_manifests(workspace):
        # Skill name is the parent folder; match exactly (case-sensitive, same as fs).
        if manifest.parent.name == skill_name:
            summary = _summarize(manifest, workspace)
            try:
                content = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                log.warning("Failed to read %s: %s", manifest, e)
                raise HTTPException(status_code=500, detail="failed to read skill file") from e
            return AgentSkillDetailOut(
                **summary.model_dump(),
                content=content,
            )
    raise HTTPException(status_code=404, detail="skill not found")


# ---------------------------------------------------------------------------
# Dashboard helper: cheap count of skills per agent
# ---------------------------------------------------------------------------

def count_skills_for(agent) -> int | None:
    """Fast count for the dashboard badge. Returns None if workspace is unset.

    Filesystem-only \u2014 no parsing, just counts manifest files. Idiomatic for
    a list view where we don't need any content yet.
    """
    workspace = _resolve_workspace(agent)
    if workspace is None or not workspace.exists():
        return None
    try:
        return sum(1 for _ in _iter_skill_manifests(workspace))
    except OSError as e:
        log.debug("skill count failed for agent %s: %s", agent.id, e)
        return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_workspace(agent) -> Path | None:
    """Resolve and validate the agent's workspace path.

    Returns ``None`` if the agent has no workspace_path or it's empty.
    """
    if not agent.workspace_path:
        return None
    p = Path(agent.workspace_path).resolve()
    return p


def _iter_skill_manifests(workspace: Path):
    """Yield every ``SKILL.md`` file under ``workspace``.

    Looks in both supported layouts:

    1. ``workspace/skills/*/SKILL.md`` (canonical)
    2. ``workspace/*/SKILL.md``         (some legacy agents)

    De-duplicates by skill folder name (canonical layout wins on collision).
    Skips dotfolders so things like ``.git`` don't pollute the list.
    """
    seen: set[str] = set()

    skills_dir = workspace / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            manifest = child / "SKILL.md"
            if manifest.is_file() and _is_under(manifest, workspace):
                seen.add(child.name)
                yield manifest

    # Legacy layout: SKILL.md directly in <workspace>/<name>/. We *only*
    # consider top-level directories here \u2014 don't descend into arbitrary
    # subtrees, which would let any nested folder pose as a skill.
    for child in sorted(workspace.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in ("skills",):
            continue  # already covered above
        if child.name in seen:
            continue
        manifest = child / "SKILL.md"
        if manifest.is_file() and _is_under(manifest, workspace):
            seen.add(child.name)
            yield manifest


def _is_under(path: Path, root: Path) -> bool:
    """Guard against path traversal via symlinks: resolve and verify containment."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _summarize(manifest: Path, workspace: Path) -> AgentSkillOut:
    """Parse the frontmatter (best-effort) and build a summary row.

    Falls back gracefully if the frontmatter is missing or malformed \u2014 we
    still surface the skill (by folder name) so the owner can investigate.
    """
    name_from_dir = manifest.parent.name
    frontmatter = _parse_frontmatter(manifest)

    # Frontmatter "name" wins if present; otherwise fall back to folder name.
    name = _coerce_str(frontmatter.get("name")) or name_from_dir

    # ``description`` may be multi-line YAML literal \u2014 normalize whitespace
    # so the dashboard tooltip renders nicely without line breaks.
    description = _coerce_str(frontmatter.get("description"))
    if description:
        description = " ".join(description.split())

    metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}

    return AgentSkillOut(
        name=name,
        description=description,
        version=_coerce_str(metadata.get("version")),
        author=_coerce_str(metadata.get("author")),
        # ``updated`` is often a YAML date; coerce to ISO-ish string.
        updated=_coerce_str(metadata.get("updated")),
        path=str(manifest.parent.relative_to(workspace)),
    )


def _parse_frontmatter(manifest: Path) -> dict:
    """Read and parse the YAML frontmatter block. Returns ``{}`` on any error.

    Recognised format::

        ---
        key: value
        ---
        ... markdown body ...
    """
    try:
        raw = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("Failed to read %s: %s", manifest, e)
        return {}

    if not raw.startswith("---"):
        return {}

    # Find the closing ``---`` on its own line.
    parts = raw.split("\n", 1)
    if len(parts) < 2:
        return {}
    body = parts[1]
    end_idx = body.find("\n---")
    if end_idx == -1:
        return {}

    yaml_block = body[:end_idx]
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError as e:
        log.debug("Bad YAML in %s: %s", manifest, e)
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_str(v) -> str | None:
    """Best-effort coercion for frontmatter values to displayable strings.

    YAML can return dates, ints, floats, etc. \u2014 we just stringify everything
    and let the UI render. ``None`` stays ``None`` so the schema can omit it.
    """
    if v is None:
        return None
    s = str(v).strip()
    return s or None
