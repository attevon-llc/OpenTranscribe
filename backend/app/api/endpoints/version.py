"""Build-identity endpoint: what code is actually running here?

Deliberately public and deliberately DB-free.

*Public* because ``/health`` already returns the version, so the only additional
disclosure is a short commit SHA — and the value of an unauthenticated answer is
high: the release harness, an upgrade smoke test, a load balancer, and a user
filing a bug all need it, and several of them have no credentials.

*DB-free* because it must answer when Postgres is down, which is exactly when
"what version is this?" gets asked. Schema state lives on ``/health/ready``,
which is the endpoint that already owns dependency probing.

The release harness asserts ``version`` equals the version under test. That one
assertion is what upgrades "a stack came up after the compose swap" into "the
new code is actually running" — with ``pull_policy: never`` and local tag
pinning, a silently-stale image is a genuinely reachable failure mode.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.version import APP_VERSION
from app.core.version import BUILD_TIME
from app.core.version import GIT_SHA

router = APIRouter()


class VersionResponse(BaseModel):
    """Build identity of the running backend."""

    version: str
    git_sha: str
    build_time: str
    api_version: str


@router.get("", response_model=VersionResponse, summary="Running build identity")
@router.get("/", response_model=VersionResponse, include_in_schema=False)
def get_version() -> VersionResponse:
    """Return the version, commit and build time of the running image.

    Any field may be ``"unknown"`` when the image was built without the
    corresponding ``--build-arg`` (see ``app.core.version``). A released image
    reporting ``"unknown"`` is a release-process bug, and
    ``scripts/release/check-version-consistency.py`` plus the release harness
    both assert against it.
    """
    return VersionResponse(
        version=APP_VERSION,
        git_sha=GIT_SHA,
        build_time=BUILD_TIME,
        # Bumped only on a breaking change to the API contract itself, which is
        # independent of the application version.
        api_version="1",
    )
