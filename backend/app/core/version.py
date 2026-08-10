"""Application build identity: version, git SHA, build time.

Read once at import. Three facts, three sources, all with the same fallback
shape: an explicit build argument baked into the image wins, otherwise we look
for the repo checkout (dev), otherwise "unknown".

Why the env var matters more than it looks: the backend image is built with
``context: ./backend``, so the repo-root VERSION file is **not** inside the
image. A prod container has no VERSION to find and depends entirely on
``--build-arg APP_VERSION`` being passed. ``scripts/docker-build-push.sh``
passes it; the hand-written ``docker build`` commands in the old release
checklists did not, which is why by-the-book images reported "unknown" — and
why AboutModal's version-mismatch warning silently stopped working (it
suppresses itself when the server says "unknown").

``scripts/release/check-version-consistency.py`` asserts the ARG still exists in
Dockerfile.prod so this contract cannot be quietly dropped again.
"""

from __future__ import annotations

import os
from pathlib import Path

UNKNOWN = "unknown"


def _read_version() -> str:
    """Version string, e.g. ``v0.5.0``.

    Prefers the build argument (the only source available in a prod image), then
    walks up for a VERSION file (dev checkouts and the offline package).
    """
    env_version = os.environ.get("APP_VERSION")
    if env_version and env_version != UNKNOWN:
        return env_version.strip()

    current = Path(__file__).resolve().parent
    for _ in range(6):
        candidate = current / "VERSION"
        if candidate.is_file():
            return candidate.read_text().strip()
        current = current.parent

    return UNKNOWN


def _read_git_sha() -> str:
    """Short commit SHA the image was built from, or ``unknown``.

    Never shells out to git: the prod image has no .git directory, and a
    subprocess call at import time would be a startup cost paid on every boot
    for a value that is static.
    """
    sha = os.environ.get("GIT_SHA", "").strip()
    return sha or UNKNOWN


def _read_build_time() -> str:
    """ISO-8601 UTC timestamp of the image build, or ``unknown``."""
    built = os.environ.get("BUILD_TIME", "").strip()
    return built or UNKNOWN


APP_VERSION = _read_version()
GIT_SHA = _read_git_sha()
BUILD_TIME = _read_build_time()
