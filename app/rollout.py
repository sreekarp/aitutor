"""Which prompts and models a session uses. A new session saves the live ones, or a staged rollout's for a stable
share of students; every turn then uses what its session saved, so a session ends on the versions it began with."""
from __future__ import annotations

import hashlib

from app import config

ROLES = ("understand", "respond")


def cohort(rollout_id: str, student_id: str) -> int:
    """0-99, the same every time for this rollout and student; a new rollout id draws a new share of students."""
    return int(hashlib.sha256(f"{rollout_id}:{student_id}".encode()).hexdigest()[:8], 16) % 100


def choose(student_id: str) -> tuple[dict, dict]:
    """The prompt versions and models a new session saves: a rollout's for its share of students, else the live ones."""
    rollout = config.ROLLOUT
    if rollout and cohort(rollout["id"], student_id) < rollout["percent"]:
        return dict(rollout["prompt_versions"]), rollout["models"]
    return dict(config.PROMPT_VERSIONS), config.MODELS


def pinned(session: dict) -> tuple[dict, dict]:
    """What a turn uses: the versions its session saved. The live ones instead when the session began in scripted
    mode (no models saved), or one of its prompts was retired (a rollback) or its file is gone."""
    versions, models = session["prompt_versions"] or {}, session["models"] or {}
    usable = all(role in models and versions.get(role) not in config.RETIRED_PROMPTS
                 and (config.PROMPTS_DIR / f"{versions.get(role)}.md").is_file() for role in ROLES)
    return (versions, models) if usable else (dict(config.PROMPT_VERSIONS), config.MODELS)
