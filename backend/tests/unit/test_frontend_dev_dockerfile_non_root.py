"""The dev frontend image must not run as root (issue #1372).

``docker-compose.override.yml`` bind-mounts ``./frontend`` into the container. A root-run dev
server leaves root-owned directories in the checkout, and the host's ``vite build``
(``emptyOutDir``) then dies with EACCES half way through emptying ``dist``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile.dev"


def final_user(text: str) -> str | None:
    """The user the last USER instruction selects, or None when the image runs as root."""
    users = re.findall(r"^\s*USER\s+(\S+)", text, flags=re.MULTILINE)
    return users[-1] if users else None


def root_owned_copies(text: str) -> list[str]:
    """COPY lines issued after the USER switch that do not hand the files to that user."""
    switch = re.search(r"^\s*USER\s", text, flags=re.MULTILINE)
    after = text[switch.start() :] if switch else text
    return [
        line.strip()
        for line in after.splitlines()
        if re.match(r"\s*COPY\s", line) and "--chown=" not in line
    ]


def test_dev_image_runs_as_a_non_root_user():
    user = final_user(DOCKERFILE.read_text(encoding="utf-8"))
    assert user not in (None, "root", "0"), f"frontend/Dockerfile.dev runs as {user or 'root'}"


def test_copies_after_the_user_switch_are_owned_by_that_user():
    bad = root_owned_copies(DOCKERFILE.read_text(encoding="utf-8"))
    assert not bad, f"these COPYs leave root-owned files in /app: {bad}"


def test_checks_fire_on_a_root_image_and_stay_clean_on_a_fixed_one():
    root = "FROM node\nCOPY . .\n"
    fixed = "FROM node\nUSER node\nCOPY --chown=node:node . .\n"
    leaky = "FROM node\nUSER node\nCOPY . .\n"
    assert final_user(root) is None
    assert final_user(fixed) == "node"
    assert root_owned_copies(fixed) == []
    assert root_owned_copies(root) == ["COPY . ."]
    assert root_owned_copies(leaky) == ["COPY . ."]
