"""Session preflight: refuse to run the suite against a stack whose schema is not this checkout's.

With nothing exported, ``POSTGRES_PORT`` defaults to the live dev stack's 5176. If that database
is behind or ahead of this checkout's Alembic head, a few dozen tests fail with errors that read
like product bugs (backend/tests/CLAUDE.md records the 13-failure case). The check itself lives in
``scripts/lib/stack_target.py`` so shell scripts and pytest cannot disagree about what "the right
stack" means; this module only decides WHETHER it applies.

Not applicable (one line printed, run continues): docker is unreachable, the port is published by
no compose container (CI service postgres, nothing listening), or the owner is a throwaway
``opentranscribe-pytest-*`` database (throwaway-pg.sh already migrated it to head).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STACK_TARGET_PATH = _REPO_ROOT / "scripts" / "lib" / "stack_target.py"
THROWAWAY_PREFIX = "opentranscribe-pytest-"
EXIT_CODE = 3


def load_stack_target() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stack_target", _STACK_TARGET_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("stack_target", module)
    spec.loader.exec_module(module)
    return module


def run_preflight(env: Mapping[str, str] | None = None, repo_root: Path = _REPO_ROOT) -> str | None:
    """Return None when the suite may run, else the one-line reason it must not.

    Informational "not applicable" lines are printed to stderr and also return None.
    """
    env = dict(os.environ if env is None else env)
    st = load_stack_target()
    port = env.get("POSTGRES_PORT", "")
    if not port.isdigit():
        return None
    try:
        owner = st.port_owner(port)
    except st.StackError:
        print("stack-preflight: not applicable (docker unavailable)", file=sys.stderr)
        return None
    if not owner or owner.startswith(THROWAWAY_PREFIX):
        print(
            f"stack-preflight: not applicable (POSTGRES_PORT={port} owner='{owner}')",
            file=sys.stderr,
        )
        return None
    requirements = ["schema-head"]
    if env.get("OT_STACK_REQUIRE_CODE") == "bind":
        requirements.append("code=bind")
    try:
        stack = st.resolve(repo_root, project=owner, env=env)
        allow = st.parse_allow([], env.get("ST_ALLOW", ""))
        failures = st.run_checks(stack, requirements, allow)
    except st.StackError as exc:
        return str(exc.line())
    return str(failures[0].line()) if failures else None


# ---------------------------------------------------------------------------- e2e (plan P3)


def _port_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return str(parsed.port or "")


def observed_ports(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """Published host ports of the stack the environment names, or {} when none resolves.

    The e2e defaults used to be the literals 5173/5174, which is the live stack on whatever
    checkout runs the suite. Resolving through compose labels names the stack that is really
    there; {} (no docker, no stack, ambiguity) lets the caller refuse instead of guessing.
    """
    env = dict(os.environ if env is None else env)
    st = load_stack_target()
    try:
        return dict(st.resolve(_REPO_ROOT, env=env).ports)
    except st.StackError:
        return {}


def stack_port(var: str, env: Mapping[str, str] | None = None) -> str:
    """Host port for ``var``: the exported value, else the resolved stack's observed port.

    "" when nothing resolves; a live test then skips or fails instead of probing the stock port
    of whichever stack happens to hold it.
    """
    env = dict(os.environ if env is None else env)
    return env.get(var) or str(observed_ports(env).get(var, ""))


def resolved_urls(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(frontend, backend) base URLs: the exported E2E_* values, else the resolved stack's ports.

    "" for one that nothing resolves, so the caller refuses instead of guessing the live port.
    """
    env = dict(os.environ if env is None else env)
    ports = observed_ports(env)

    def pick(var: str, port_var: str) -> str:
        if env.get(var):
            return env[var]
        return f"http://localhost:{ports[port_var]}" if port_var in ports else ""

    return pick("E2E_FRONTEND_URL", "FRONTEND_PORT"), pick("E2E_BACKEND_URL", "BACKEND_PORT")


def missing_stack_problem(base_url: object, backend_url: object) -> str | None:
    if base_url and backend_url:
        return None
    return "no stack resolved (stack-target NO_PROJECT(20)): ./opentr.sh start dev"


def url_owner(url: str) -> str:
    """Compose project publishing the host port of *url*, '' when none (or docker is down)."""
    port = _port_of(url)
    st = load_stack_target()
    try:
        return str(st.port_owner(port)) if port else ""
    except st.StackError:
        return ""


def is_isolated_stack(url: str) -> bool:
    """True when *url* is served by a ``--fresh`` (``otfresh-*``) project, never a shared one."""
    return url_owner(url).startswith("otfresh-")


def e2e_stack_problem(
    base_url: str, backend_url: str, env: Mapping[str, str] | None = None
) -> str | None:
    """Why the e2e target is not one healthy stack carrying the assets the suite needs, or None.

    Both URLs must be published by the SAME compose project (the ownership cross-check), which
    must be healthy and have the gitignored fonts a worktree lacks. ``code=bind`` is added when
    ``OT_STACK_REQUIRE_CODE=bind`` (run-e2e.sh's dev default). Not applicable, one line printed,
    when docker is down or neither port belongs to a compose project (CI, a remote stack).
    """
    env = dict(os.environ if env is None else env)
    base_owner, backend_owner = url_owner(base_url), url_owner(backend_url)
    if not base_owner and not backend_owner:
        print("stack-preflight: e2e not applicable (no compose owner)", file=sys.stderr)
        return None
    if base_owner != backend_owner or not base_owner:
        return (
            f"stack-target: FAIL WRONG_PROJECT(21): {base_url} is published by "
            f"'{base_owner or 'nothing'}' but {backend_url} by '{backend_owner or 'nothing'}'. "
            "Fix: point both at one project"
        )
    requirements = ["healthy", "asset:frontend-fonts"]
    if env.get("OT_STACK_REQUIRE_CODE") == "bind":
        requirements.append("code=bind")
    st = load_stack_target()
    try:
        stack = st.resolve(_REPO_ROOT, project=base_owner, env=env)
        failures = st.run_checks(stack, requirements, st.parse_allow([], env.get("ST_ALLOW", "")))
    except st.StackError as exc:
        return str(exc.line())
    return str(failures[0].line()) if failures else None


def resolved_port_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Host ports the target stack OBSERVABLY publishes (``POSTGRES_PORT``, ``MINIO_PORT``, ...).

    Target = ``COMPOSE_PROJECT_NAME`` if exported, else the one live project; an exported
    ``POSTGRES_PORT`` is deliberately ignored here (the caller only asks when it is unset or the
    in-container 5432). Empty when docker is unreachable or no single stack resolves: the caller
    then leaves the variable unset instead of guessing the stock port.
    """
    scrubbed = {k: v for k, v in dict(os.environ if env is None else env).items()}
    scrubbed.pop("POSTGRES_PORT", None)
    return {name: str(port) for name, port in observed_ports(scrubbed).items()}
