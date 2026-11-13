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
