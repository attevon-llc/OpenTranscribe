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
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONFIG_PY = REPO_ROOT / "backend" / "app" / "core" / "config.py"
APP_DIR = REPO_ROOT / "backend" / "app"

pytestmark = pytest.mark.skipif(
    not ENV_EXAMPLE.exists(), reason=".env.example not present in this checkout"
)

# Documented keys that are deliberately consumed by nothing IN THIS REPO — either
# read by an external consumer (a user's reverse proxy, an operator's tooling), or
# a reserved/compliance-intent setting with a written reason it stays even though
# no code path reads it yet.
#
# Keep this list short. It is an admission, not a category.
EXTERNALLY_CONSUMED: set[str] = {
    # FedRAMP AC-2 control: kept undeleted on purpose (audit group C3) even though
    # nothing in production reads it yet. test_fedramp_controls.py asserts it exists
    # and is >= 90, which documents intended future enforcement rather than a stray
    # env var — deleting it would silently regress that control's groundwork.
    "AUDIT_LOG_RETENTION_DAYS",
}

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


#: Local wrapper functions (K2) whose first string-literal argument is an env var
#: name, beside the stdlib/oidc names the raw-call scan already recognised. A call to
#: one of these was invisible to the scan, which produced a false finding for
#: GPU_SCALE_ENABLED / GPU_SCALE_DEFAULT_WORKER (``tasks/utility.py``'s ``_env_flag``)
#: the first time this test ran against them.
_ENV_READ_WRAPPER_NAMES = {
    "getenv",
    "get",
    "oidc_env",
    "oidc_bool_env",
    "oidc_int_env",
    "_int_env",
    "_env_flag",
}


@lru_cache(maxsize=1)
def _keys_read_by_code() -> frozenset[str]:
    """Every env var name the backend reads.

    Union of Settings field names and the string literal passed to os.getenv /
    os.environ.get / the oidc_*_env helpers / local wrappers like ``_env_flag``
    (K2), found by walking the AST rather than grepping, so a name split across
    lines or wrapped in a helper still counts.

    CACHED, and returning a frozenset so the cached value cannot be mutated by a
    caller. This walks and AST-parses every file under app/, and
    ``test_compose_vars_without_defaults_are_documented`` called it *inside* its loop
    over the 26 ``docker-compose*.yml`` files — recomputing an identical
    loop-invariant set 26 times, which made that one test 57.6 s of a 139 s suite
    (the next slowest test was 9.9 s).
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
            if called not in _ENV_READ_WRAPPER_NAMES:
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
                    names.add(value)

    return frozenset(names)


def _settings_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to the ``app.core.config`` settings singleton in one module.

    ``from app.core.config import settings as app_settings`` is a real, common pattern
    (``tags/crud.py``, ``tasks/recovery.py``) — a scan that only recognises the literal
    name ``settings`` produced false "nothing consumes this" findings for
    ``READ_CACHE_ENABLED`` and ``YOUTUBE_RECOVERY_BATCH_SIZE``, both read via an alias.
    """
    aliases = {"settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.core.config":
            for alias in node.names:
                if alias.name == "settings":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _config_py_internal_reads() -> frozenset[str]:
    """Env var names ``config.py`` reads MORE THAN ONCE via a raw ``os.getenv``-style call.

    A field's own declaration (``REDIS_USE_TLS: bool = os.getenv("REDIS_USE_TLS", ...)``)
    is one read and, alone, is mere declaration. ``REDIS_USE_TLS`` is also read a second
    time, raw, to pick ``REDIS_URL``'s scheme (``"rediss" if os.getenv("REDIS_USE_TLS", ...)
    ... else "redis"``) — genuine consumption that happens to stay inside config.py, per
    the pattern ``core/celery.py`` documents in its own comment. A second literal read of
    the same name is the signal that distinguishes this from a merely-declared, unread field.
    """
    counts: dict[str, int] = {}
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in _ENV_READ_WRAPPER_NAMES:
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value
            if isinstance(value, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
                counts[value] = counts.get(value, 0) + 1
    return frozenset(name for name, count in counts.items() if count >= 2)


@lru_cache(maxsize=1)
def _keys_genuinely_consumed() -> frozenset[str]:
    """Keys actually read by something, not merely DECLARED as a Settings field (K2).

    ``_keys_read_by_code()`` counts a bare ``FOO: str = os.getenv("FOO", ...)``
    Settings field declaration in config.py as "read" regardless of whether anything
    outside config.py ever accesses ``settings.FOO`` — which is exactly how
    ``UPLOAD_DIR``, ``GPU_CLUSTERING_DEVICE`` and three ASR credential settings (C1-C4)
    went undetected by this test: each was declared, so the union in
    ``test_documented_keys_are_actually_consumed`` already contained the name before
    this function existed. This is the narrower signal that function was missing:
    ``settings.FOO`` / ``getattr(settings, "FOO", ...)`` read OUTSIDE config.py (through
    any import alias — see ``_settings_aliases``), a raw env-var read of the same name
    outside config.py (the ``services/asr/factory.py`` pattern — reads the env var
    directly, bypassing the Settings field it shares a name with), or a SECOND raw read
    of the same name inside config.py itself (``_config_py_internal_reads`` — a field
    that config.py derives another field from, never accessed as ``settings.FOO``
    anywhere).
    """
    names: set[str] = set(_config_py_internal_reads())
    for path in APP_DIR.rglob("*.py"):
        if path == CONFIG_PY:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        aliases = _settings_aliases(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                names.add(node.attr)
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = ""
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            if called == "getattr" and len(node.args) >= 2:
                target, attr_arg = node.args[0], node.args[1]
                if (
                    isinstance(target, ast.Name)
                    and target.id in aliases
                    and isinstance(attr_arg, ast.Constant)
                    and isinstance(attr_arg.value, str)
                ):
                    names.add(attr_arg.value)
                continue
            if called in _ENV_READ_WRAPPER_NAMES and node.args:
                if isinstance(node.args[0], ast.Constant):
                    value = node.args[0].value
                    if isinstance(value, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
                        names.add(value)

    return frozenset(names)


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
    consumed = _keys_genuinely_consumed() | _keys_consumed_on_the_surface()
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
