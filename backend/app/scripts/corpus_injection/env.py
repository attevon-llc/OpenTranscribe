"""Environment bootstrap — must run before anything imports ``app.core.config``.

Two jobs, both of which have to happen before the settings module is imported
because it reads ``os.environ`` at class-definition time:

1. **Load the repo-root ``.env``.** ``Settings``' ``env_file = ".env"`` is
   relative to the process CWD, so running the injector from anywhere other
   than the repo root silently falls back to the built-in defaults — which is
   how a run can end up authenticating as ``postgres/postgres`` against the
   wrong database and reporting "password authentication failed" for a stack
   that is perfectly healthy. ``tests/conftest.py`` already does this dance;
   this is the same fix for a script.
2. **Point the data directories somewhere writable.** ``Settings.__init__``
   mkdirs ``UPLOAD_DIR``, which is ``/app/data/uploads`` inside the container
   and unwritable on the host.

It also holds the live-stack guard. The injector writes hundreds of rows and
thousands of OpenSearch documents; doing that to the shared dev stack would
pollute somebody's real library. So a target that looks like the dev stack is
refused unless it is named explicitly.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# The dev stack's host port mappings (docker-compose.override.yml). Injecting
# into these is almost always a mistake — use ./opentr.sh start dev --fresh.
LIVE_DEV_PORTS = {"POSTGRES_PORT": "5176", "OPENSEARCH_PORT": "5180", "MINIO_PORT": "5178"}

_ENV_KEYS = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "OPENSEARCH_HOST",
    "OPENSEARCH_PORT",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "MINIO_HOST",
    "MINIO_PORT",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASSWORD",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "MEDIA_BUCKET_NAME",
)


class LiveStackRefusedError(RuntimeError):
    """Raised when the resolved target looks like the shared dev stack."""


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for the directory holding ``opentr.sh``."""
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "opentr.sh").is_file():
            return candidate
    return None


def bootstrap(repo_root: Path | None = None) -> Path | None:
    """Load ``.env`` (without overriding the shell) and fix the data dirs.

    Explicitly exported variables always win: the wrapper script sets the
    isolated stack's ports that way, and they must not be clobbered by the
    ``.env`` a shared checkout happens to carry.
    """
    root = repo_root or find_repo_root()
    if root is not None:
        env_file = root / ".env"
        if env_file.is_file():
            from dotenv import dotenv_values

            values = dotenv_values(env_file)
            for key in _ENV_KEYS:
                value = values.get(key)
                if value:
                    os.environ.setdefault(key, value)

    scratch = Path(tempfile.gettempdir()) / "opentranscribe-corpus-injection"
    for var, sub in (("DATA_DIR", "data"), ("MODELS_DIR", "models"), ("TEMP_DIR", "temp")):
        path = scratch / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault(var, str(path))
    return root


def describe_target() -> dict[str, str]:
    """The resolved target, for the console banner and the manifest."""
    from app.core.config import settings

    return {
        "postgres": f"{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}",
        "opensearch": f"{settings.OPENSEARCH_HOST}:{settings.OPENSEARCH_PORT}",
        "opensearch_chunks_index": settings.OPENSEARCH_CHUNKS_INDEX,
    }


def guard_live_stack(allow: bool = False) -> None:
    """Refuse a target that matches the shared dev stack's port mapping."""
    if allow:
        return
    matches = [
        f"{var}={os.environ.get(var)}"
        for var, port in LIVE_DEV_PORTS.items()
        if os.environ.get(var) == port
    ]
    if matches:
        raise LiveStackRefusedError(
            "Refusing to inject into what looks like the shared dev stack "
            f"({', '.join(matches)}). Eval corpora belong in an isolated deployment: "
            "`./opentr.sh start dev --fresh <name> --port-offset N`, then export the "
            "offset ports. Pass --allow-live-stack only if you genuinely mean the dev stack."
        )
