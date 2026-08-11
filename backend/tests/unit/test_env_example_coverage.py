"""Keep `.env.example` honest about what the code actually reads.

`Settings` uses ``extra="ignore"`` (see backend/app/core/CLAUDE.md), which means a
typo'd or removed environment variable is silently dropped rather than crashing
startup. That is a deliberate robustness choice, and it is exactly why the
template needs a test: nothing else notices when the template and the code
disagree.

Scope is deliberately narrow. The repo prefers DB-backed ``SystemSettings`` with
coded defaults in ``core/constants.py`` over new env vars, so this does NOT
demand that every setting be documented. It checks three things:

1. Documented keys are actually read somewhere — a key nobody reads is either a
   typo or a leftover from a removed feature, and either way it misleads.
2. A curated set of operationally significant keys IS documented. Curated, not
   exhaustive: these are the ones whose absence from the template has bitten.
3. Every ``${VAR}`` a compose file interpolates *without* a default is present,
   because that is the one case where a missing key breaks the deployment rather
   than silently defaulting.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONFIG_PY = REPO_ROOT / "backend" / "app" / "core" / "config.py"
APP_DIR = REPO_ROOT / "backend" / "app"

pytestmark = pytest.mark.skipif(
    not ENV_EXAMPLE.exists(), reason=".env.example not present in this checkout"
)

# Documented keys that are deliberately consumed by nothing IN THIS REPO — they
# are read by an external consumer (a user's reverse proxy, an operator's
# tooling). Anything else absent from the whole deployment surface is dead
# documentation and should be deleted from .env.example.
#
# Keep this list short. It is an admission, not a category.
EXTERNALLY_CONSUMED: set[str] = set()

# Files that make up the deployment surface. A documented key is "consumed" if it
# appears in the backend AST scan OR anywhere in these — compose interpolation,
# an image's own entrypoint env, a host script, or the frontend build.
SURFACE_GLOBS = (
    "docker-compose*.yml",
    "opentr.sh",
    "opentranscribe.sh",
    "setup-opentranscribe.sh",
    "scripts/**/*.sh",
    "scripts/**/*.py",
    "nginx/**/*",
    "frontend/*.ts",
    "frontend/*.js",
    "frontend/src/**/*.ts",
    "frontend/src/**/*.svelte",
    "monitoring/**/*",
)

# Operationally significant keys that MUST stay documented. Curated on purpose.
MUST_BE_DOCUMENTED = {
    # Changes readiness semantics; was reachable only by reading config.py.
    "RUN_MIGRATIONS_ON_STARTUP",
    # Fail-closed security gate; was dead code for months because nothing set it.
    "ENVIRONMENT",
    # Each fronts a real operational or cost decision.
    "ALLOW_OPEN_REGISTRATION",
    "MAX_UPLOAD_BYTES",
    "HUGGINGFACE_TOKEN",
}


def _documented_keys() -> set[str]:
    """Keys in .env.example, including commented-out `# KEY=value` examples."""
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lstrip("#").strip()
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=", stripped)
        if match:
            keys.add(match.group(1))
    return keys


def _keys_read_by_code() -> set[str]:
    """Every env var name the backend reads.

    Union of Settings field names and the string literal passed to os.getenv /
    os.environ.get / the oidc_*_env helpers, found by walking the AST rather than
    grepping, so a name split across lines or wrapped in a helper still counts.
    """
    names: set[str] = set()

    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # Settings field declarations: `FOO: str = os.getenv(...)`
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)

    for path in APP_DIR.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = ""
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            if called not in {"getenv", "get", "oidc_env", "oidc_bool_env", "oidc_int_env"}:
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
                    names.add(value)

    return names


def _keys_consumed_on_the_surface() -> set[str]:
    """Every env var name mentioned anywhere in the deployment surface.

    Plain substring scan on purpose: compose interpolation (``${FOO}``), an
    image's ``FOO: ${FOO}`` env mapping, and a shell ``$FOO`` are all valid
    consumption and are not worth three separate parsers.
    """
    seen: set[str] = set()
    token = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
    for pattern in SURFACE_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file():
                continue
            try:
                seen.update(token.findall(path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:  # pragma: no cover - defensive
                continue
    return seen


def test_documented_keys_are_actually_consumed():
    """A documented key that nothing anywhere reads is dead documentation.

    Caught two real ones on introduction: MIGRATION_GPU_WORKERS and
    MIGRATION_MAX_CONCURRENT_TASKS appeared only in .env.example — no compose
    file, no script, no application code.
    """
    consumed = _keys_read_by_code() | _keys_consumed_on_the_surface()
    unread = _documented_keys() - consumed - EXTERNALLY_CONSUMED
    assert not unread, (
        f"{len(unread)} key(s) documented in .env.example that NOTHING in the repo "
        f"consumes: {sorted(unread)}. Delete them from .env.example, fix the "
        "spelling, or add them to EXTERNALLY_CONSUMED with a reason."
    )


def test_operationally_significant_keys_are_documented():
    missing = MUST_BE_DOCUMENTED - _documented_keys()
    assert not missing, f"these must stay documented in .env.example: {sorted(missing)}"


def test_compose_vars_without_defaults_are_documented():
    """`${VAR}` with no `:-default` breaks the deployment when unset.

    This is the actual compose<->env contract, and it generalises
    test_proxy_trust_overlays.py, whose own docstring calls itself "the cheap
    version of the control".
    """
    # Variables Docker/compose provide or that an orchestrator sets on the
    # command line — never something a user writes into .env.
    docker_native = {"COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "DOCKER_RUNTIME"}

    documented = _documented_keys()
    undocumented: dict[str, set[str]] = {}

    for compose_file in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        # Strip comments first: a `${VAR}` inside an explanatory comment is prose,
        # not interpolation, and flagging it trains people to ignore this test.
        text = "\n".join(
            line
            for line in compose_file.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        # ${VAR} but NOT ${VAR:-default} / ${VAR-default} / ${VAR:?err}
        referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", text))
        missing = referenced - documented - _keys_read_by_code() - docker_native
        if missing:
            undocumented[compose_file.name] = missing

    assert not undocumented, (
        "compose files interpolate variables with no default and no .env.example "
        f"entry: { {k: sorted(v) for k, v in undocumented.items()} }"
    )
