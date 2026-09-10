"""The dev backend's hot-reload watcher must be scoped to ``app/``, not the whole tree.

``docker-compose.override.yml`` bind-mounts ``./backend:/app`` and ran a bare
``uvicorn --reload``, so the watcher covered **all** of ``backend/`` — tests, fixtures,
alembic, everything. Observed live::

    WARNING:  WatchFiles detected changes in 'tests/fixtures/overlay_lib_harness.py'. Reloading...
    INFO:     Started server process [1544]        # 15 SECONDS LATER

Fifteen seconds of a socket that accepts and does not answer, caused by editing a *test*
file. The reloader parent holds the listening socket, so this is a HUNG request rather than a
refused one — and ``frontend/src/routes/+layout.svelte`` renders the entire app behind
``{#if $authReady}``, which is set only once ``initAuth``'s ``GET /auth/session`` resolves. A
refused connection still renders the login form; a hung one leaves the page blank. That is the
shape of the most common e2e failure: 13 of 26 failures in one run were
``wait_for_selector: Timeout 30000ms`` on ``.gallery-action-buttons`` (8) or ``#email`` (4),
both app-shell selectors mounted unconditionally.

Verified empirically before this test was written, with a throwaway ASGI app: with
``--reload-dir app`` a ``tests/`` edit produced no reload and an ``app/`` edit did; with a
bare ``--reload`` the same ``tests/`` edit reloaded the server.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERRIDE = REPO_ROOT / "docker-compose.override.yml"

pytestmark = pytest.mark.skipif(
    not OVERRIDE.is_file(), reason="docker-compose.override.yml is not present in this checkout"
)


def _reloading_services() -> dict[str, list[str]]:
    """Every service in the dev override whose command enables uvicorn's reloader."""
    compose = yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))
    found: dict[str, list[str]] = {}
    for name, service in (compose.get("services") or {}).items():
        command = service.get("command")
        if not command:
            continue
        tokens = command if isinstance(command, list) else shlex.split(str(command))
        if "--reload" in tokens:
            found[name] = tokens
    return found


def test_the_dev_override_still_has_a_reloading_service():
    """Guard the guard: the checks below are vacuous if nothing hot-reloads."""
    services = _reloading_services()
    assert services, (
        "no service in docker-compose.override.yml enables --reload — either hot reload was "
        "removed (delete this file) or the command moved somewhere this test cannot see it"
    )
    assert "backend" in services, f"expected the backend service to hot-reload, found {services}"


def test_every_reloading_service_scopes_its_watch_root():
    """A bare ``--reload`` over a ``./backend:/app`` mount watches the entire test tree."""
    unscoped = [
        name for name, tokens in _reloading_services().items() if "--reload-dir" not in tokens
    ]
    assert not unscoped, (
        f"these dev services watch the whole bind-mounted tree: {unscoped}. Editing any .py "
        "under backend/ — including an e2e test file — then restarts the API mid-run."
    )


def test_the_watch_root_is_the_application_package():
    """``--reload-dir`` pointing back at the tree root would satisfy the check above."""
    services = _reloading_services()
    # Outside the loop on purpose: with no reloading service the loop body never runs and
    # every assertion in it is vacuous — a green test proving nothing.
    assert services, "no reloading service to check"
    for name, tokens in services.items():
        dirs = [tokens[i + 1] for i, token in enumerate(tokens) if token == "--reload-dir"]
        assert dirs, f"{name} lost its --reload-dir value"
        assert dirs == ["app"], (
            f"{name} watches {dirs}; it must watch only the application package ('app', "
            "relative to the image's /app WORKDIR), or test/fixture edits restart it again"
        )


def test_the_source_mount_that_makes_this_necessary_is_still_there():
    """The whole hazard is that the watcher's root is a bind mount of the repo's backend/."""
    compose = yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))
    volumes = compose["services"]["backend"].get("volumes") or []
    assert any(str(v).startswith("./backend:/app") for v in volumes), (
        "backend no longer bind-mounts ./backend:/app — re-derive whether --reload-dir is "
        "still the right scoping before changing it"
    )
