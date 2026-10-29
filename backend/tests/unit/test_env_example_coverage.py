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
    # Read in production, but through app.core.startup_validation.service_credential_settings
    # (a second BaseSettings object), not the app.core.config singleton this scanner follows.
    "ALLOW_UNENCRYPTED_OBJECT_STORE",
    # ---------------------------------------------------------------------------
    # The next 14 ARE genuinely read in production, but only via
    # AuthConfigService.ENV_TO_CONFIG_MAPPING (app/services/auth_config_service.py):
    # `getattr(settings, AuthConfigService.env_var_for(key), None)` resolves the attribute
    # name from a MAPPING VALUE at runtime, not a literal string constant, so this
    # scanner's AST walk (which requires a literal string argument) cannot see the read.
    # Each name is a live key in ENV_TO_CONFIG_MAPPING (verified by grep).
    "ACCOUNT_LOCKOUT_ENABLED",
    "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES",
    "ACCOUNT_LOCKOUT_PROGRESSIVE",
    "CONCURRENT_SESSION_POLICY",
    "MAX_CONCURRENT_SESSIONS",
    "MFA_BACKUP_CODE_COUNT",
    "MFA_ISSUER_NAME",
    "PASSWORD_HISTORY_COUNT",
    "PASSWORD_MAX_AGE_DAYS",
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_REQUIRE_DIGIT",
    "PASSWORD_REQUIRE_LOWERCASE",
    "PASSWORD_REQUIRE_SPECIAL",
    "PASSWORD_REQUIRE_UPPERCASE",
    # Same mechanism, new for the password-policy profiles (issue #1107): read by
    # `get_process_auth_settings().get("password_<x>")` -> `getattr(settings, key.upper())`
    # in app/auth/password_policy.py, so the attribute name is built at runtime.
    "PASSWORD_BLOCKLIST_ENABLED",
    "PASSWORD_CONTEXT_WORDS",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_POLICY_PROFILE",
    # Read via `settings.watch_temp_dir` (services/watch_sources/processing.py,
    # tasks/watch_source_tasks.py) — a lowercase config.py PROPERTY that internally reads
    # `self.WATCH_TEMP_DIR`. The scanner records the attribute name it sees on the call
    # site ("watch_temp_dir"), which never matches the uppercase field name documented
    # in .env.example, so a real read is invisible to it.
    "WATCH_TEMP_DIR",
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
#: GPU_SCALE_ENABLED / GPU_SCALE_DEFAULT_WORKER (``app.core.env.env_bool``)
#: the first time this test ran against them.
#:
#: ⚠️ Hand-maintained, so a NEW wrapper reads as "nothing consumes this key" — a false
#: finding whose obvious-looking fix is to delete a live variable from ``.env.example``.
#: ``test_every_env_read_wrapper_is_registered`` below fails when one is missing.
_ENV_READ_WRAPPER_NAMES = {
    "getenv",
    "get",
    "oidc_env",
    "oidc_bool_env",
    "oidc_int_env",
    "env_int",
    "env_float",
    "env_bool",
    "_raw",
}


@lru_cache(maxsize=1)
def _keys_read_by_code() -> frozenset[str]:
    """Every env var name the backend reads.

    Union of Settings field names and the string literal passed to os.getenv /
    os.environ.get / the oidc_*_env helpers / the ``app.core.env`` readers
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


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "LITERAL"`` bindings, so ``os.environ.get(NAME)`` resolves.

    ``services/asr/aws_provider.py`` names its env vars once (``BUCKET_ENV = "AWS_TRANSCRIBE_BUCKET"``)
    and reads them through the constant; a scan that only accepted a literal first argument
    reported both as unconsumed.
    """
    constants: dict[str, str] = {}
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            constants[stmt.targets[0].id] = stmt.value.value
    return constants


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
        constants = _module_string_constants(tree)
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
                first = node.args[0]
                value = None
                if isinstance(first, ast.Constant):
                    value = first.value
                elif isinstance(first, ast.Name):
                    value = constants.get(first.id)
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


def _wrapper_functions_reading_their_first_argument() -> set[str]:
    """Names of functions under ``app/`` that read ``os.environ`` keyed by their own
    first parameter — i.e. every local env-read wrapper, found rather than listed.

    Restricted to the direct shape (``os.getenv(key, ...)`` /
    ``os.environ.get(key, ...)``). The ``oidc_*`` family delegates through another
    helper instead and is not discovered here, which is why
    :data:`_ENV_READ_WRAPPER_NAMES` still carries entries this cannot regenerate.
    """
    found: set[str] = set()
    for path in (REPO_ROOT / "backend" / "app").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - defensive
            continue
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or not func.args.args:
                continue
            first_param = func.args.args[0].arg
            for node in ast.walk(func):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                target = node.func
                if not isinstance(target, ast.Attribute) or target.attr not in {"getenv", "get"}:
                    continue
                receiver = target.value
                if isinstance(receiver, ast.Name):
                    receiver_name = receiver.id
                elif isinstance(receiver, ast.Attribute):
                    receiver_name = receiver.attr
                else:
                    receiver_name = ""
                if receiver_name not in {"os", "environ"}:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id == first_param:
                    found.add(func.name)
    return found


def test_the_env_wrapper_scanner_finds_the_wrappers_we_know_exist():
    """Guard the guard: a scanner that matches nothing would pass everything below."""
    found = _wrapper_functions_reading_their_first_argument()

    assert {"_raw"} <= found, sorted(found)


def test_every_env_read_wrapper_is_registered():
    """A new wrapper nobody adds to _ENV_READ_WRAPPER_NAMES makes every key it reads
    look unconsumed — and the obvious-looking fix for THAT is to delete a live
    variable from .env.example.

    Caught on introduction: ``_float_env`` (``core/celery.py``, issue #631) made
    ``CELERY_PROC_ALIVE_TIMEOUT`` — read on the very next line — report as documented
    but consumed by nothing.
    """
    unregistered = _wrapper_functions_reading_their_first_argument() - _ENV_READ_WRAPPER_NAMES

    assert not unregistered, (
        f"env-read wrapper(s) missing from _ENV_READ_WRAPPER_NAMES: {sorted(unregistered)}. "
        "Every key read through one is invisible to this module's consumption scan."
    )


def _settings_field_names() -> frozenset[str]:
    """Annotated fields of the ``Settings`` class in ``core/config.py``."""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return frozenset(
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            )
    raise AssertionError("class Settings not found in core/config.py")  # pragma: no cover


#: Modules that may read the process environment for a Settings field name, with the reason.
RAW_READ_OF_SETTINGS_FIELD_ALLOWED: dict[str, str] = {}


def _raw_env_reads_outside_config() -> dict[str, set[str]]:
    """``{relative path: names}`` read through an env call anywhere but config.py / env.py."""
    sanctioned = {CONFIG_PY, APP_DIR / "core" / "env.py"}
    found: dict[str, set[str]] = {}
    for path in APP_DIR.rglob("*.py"):
        if path in sanctioned:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called in _ENV_READ_WRAPPER_NAMES and called != "get":
                    first = node.args[0]
                    name = (
                        first.value
                        if isinstance(first, ast.Constant)
                        else constants.get(first.id)
                        if isinstance(first, ast.Name)
                        else None
                    )
                elif (
                    called == "get"
                    and isinstance(func, ast.Attribute)
                    and ast.unparse(func.value) in {"os.environ", "_os.environ", "environ"}
                    and isinstance(node.args[0], ast.Constant)
                ):
                    name = node.args[0].value
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Load)
                and ast.unparse(node.value) in {"os.environ", "_os.environ", "environ"}
                and isinstance(node.slice, ast.Constant)
            ):
                name = node.slice.value
            if isinstance(name, str):
                found.setdefault(str(path.relative_to(REPO_ROOT)), set()).add(name)
    return found


def test_no_settings_field_is_read_raw_outside_config():
    """A name that is a ``Settings`` field has ONE source of truth: ``settings.NAME``.

    A second raw read (``os.getenv("NAME", other_default)``) is how code and Settings came to
    disagree on ``POSTGRES_HOST`` and ``GPU_DEVICE_ID`` defaults.
    """
    fields = _settings_field_names()
    assert len(fields) > 100, "scanner found too few Settings fields to mean anything"
    offenders = {
        path: sorted(names & fields)
        for path, names in _raw_env_reads_outside_config().items()
        if names & fields and path not in RAW_READ_OF_SETTINGS_FIELD_ALLOWED
    }
    assert not offenders, f"raw env read of a Settings field outside config.py: {offenders}"


def test_the_raw_read_scanner_sees_a_known_raw_read():
    """Guard the guard: ``celery.py`` reads ``SKIP_CELERY`` raw, so the scan must report it."""
    reads = _raw_env_reads_outside_config()
    assert any("SKIP_CELERY" in names for names in reads.values()), sorted(reads)[:5]


#: Compose ``environment:`` names on a backend-image service that the Python code does not read
#: itself because a LIBRARY or the runtime consumes them. Each carries the reason.
LIBRARY_CONSUMED_COMPOSE_ENV: dict[str, str] = {
    "CUDA_VISIBLE_DEVICES": "CUDA runtime device mask",
    "MKL_NUM_THREADS": "Intel MKL thread cap; the redaction worker sets it for torch/numpy",
    "PYANNOTE_METRICS_ENABLED": "pyannote telemetry switch (app/core/privacy_env.py)",
    "HF_HUB_DISABLE_TELEMETRY": "huggingface_hub telemetry switch (app/core/privacy_env.py)",
    "DO_NOT_TRACK": "third-party telemetry opt-out (app/core/privacy_env.py)",
    "DENO_NO_UPDATE_CHECK": "deno update-check switch for yt-dlp's JS runtime",
}

#: Compose ``environment:`` names that NOTHING reads. They are mistakes in a compose file that
#: ships in the release manifest, so the edit waits until after the release.
COMPOSE_ENV_READ_BY_NOTHING: dict[str, str] = {
    "MODELS_DIRECTORY": "compose edit post-release: five services set it; no code reads it",
    "REDACTION_CONCURRENCY": (
        "compose edit post-release: only the worker command's ${REDACTION_CONCURRENCY} "
        "interpolation uses it, the environment line is inert"
    ),
    "FLOWER_USER": (
        "compose edit post-release: only flower's --basic_auth interpolation uses it, "
        "the environment line is inert"
    ),
}


def _is_backend_image_service(name: str) -> bool:
    """Services that run the backend image (API, every celery worker, flower)."""
    return name == "backend" or name.startswith("celery-") or name == "flower"


def _compose_environment_names() -> dict[str, set[str]]:
    """``{variable: {compose file names}}`` for each backend-image service's ``environment:``."""
    import yaml

    found: dict[str, set[str]] = {}
    for compose_file in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        services = (yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}).get(
            "services"
        ) or {}
        for name, service in services.items():
            if not isinstance(service, dict) or not _is_backend_image_service(name):
                continue
            env = service.get("environment") or []
            names = (
                {str(item).split("=", 1)[0] for item in env} if isinstance(env, list) else set(env)
            )
            for var in names:
                found.setdefault(var, set()).add(compose_file.name)
    return found


def _startup_validation_fields() -> frozenset[str]:
    """Fields of the second ``BaseSettings`` (``core/startup_validation.py``) that app code reads."""
    tree = ast.parse((APP_DIR / "core" / "startup_validation.py").read_text(encoding="utf-8"))
    return frozenset(
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )


def test_every_compose_environment_variable_has_a_reader():
    """A variable a compose service sets on a backend container must be read by something.

    ``Settings`` is ``extra="ignore"``, so a name nobody reads is dropped silently: the compose
    line looks like configuration and does nothing (``MODELS_DIRECTORY`` did for five services).
    """
    declared = _compose_environment_names()
    assert len(declared) > 40, "compose scan found too few variables to mean anything"

    readers = _keys_read_by_code() | _keys_genuinely_consumed() | _startup_validation_fields()
    unread = {
        var: sorted(files)
        for var, files in declared.items()
        if var not in readers
        and var not in LIBRARY_CONSUMED_COMPOSE_ENV
        and var not in COMPOSE_ENV_READ_BY_NOTHING
    }
    assert not unread, (
        f"compose sets variables on backend containers that nothing reads: {unread}. "
        "Wire a reader, remove the compose line, or list it with a reason."
    )


def test_compose_environment_allowlists_have_no_stale_entries():
    """An allowlist entry that no longer applies hides the next real orphan under its name."""
    declared = _compose_environment_names()
    readers = _keys_read_by_code() | _keys_genuinely_consumed() | _startup_validation_fields()

    gone = (set(LIBRARY_CONSUMED_COMPOSE_ENV) | set(COMPOSE_ENV_READ_BY_NOTHING)) - set(declared)
    assert not gone, f"allowlisted but no longer in any compose environment: {sorted(gone)}"
    now_read = (set(LIBRARY_CONSUMED_COMPOSE_ENV) | set(COMPOSE_ENV_READ_BY_NOTHING)) & readers
    assert not now_read, f"allowlisted but code now reads them: {sorted(now_read)}"
