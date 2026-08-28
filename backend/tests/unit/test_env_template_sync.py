"""Keep `.env.example` in sync with what the app actually reads (issue #539).

A 2026-08-21 spot-check claimed six vars were read by compose/code but absent from the
template. Wrong: all six were already present as **commented** optional entries
(`# GPU_MAX_TASKS=100000`) — the spot-check's classifier missed them by counting only
uncommented `KEY=` lines (issue correction comment). So a commented `# KEY=value` line
counts as documented here too, exactly like `test_env_example_coverage.py`'s
`_documented_keys()`.

Two directions that sibling test does not cover: (a) every var **read by base
`docker-compose.yml` or `config.py`** — with or without a compose-side default — is
documented (the sibling only checks compose vars *without* a default, plus a small
curated `config.py` set); (b) every **documented** var is read by something real,
scoped tighter than the sibling (no docs/*.md).

Deliberately narrow, matching `test_shell_expansion_guards.py`'s style: only base
`docker-compose.yml` (not the 25+ dev/test overlays) and only `config.py` (not all of
`backend/app`, which pulls in every DB-backed `SystemSettings` fallback and would need
the "50-entry allowlist" this repo's CLAUDE.md warns against). A narrow scanner that
never lies beats a wide one buried in exceptions.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
CONFIG_PY = REPO_ROOT / "backend" / "app" / "core" / "config.py"

pytestmark = pytest.mark.skipif(
    not (ENV_EXAMPLE.exists() and BASE_COMPOSE.exists() and CONFIG_PY.exists()),
    reason=".env.example / docker-compose.yml / config.py not present in this checkout",
)

#: Docker/compose supplies these itself — never a `.env` entry.
_DOCKER_NATIVE = frozenset({"COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "DOCKER_RUNTIME"})

#: Local helpers that read one literal env-var name, same contract as `os.getenv`.
_ENV_HELPER_NAMES = frozenset({"oidc_env", "oidc_bool_env", "oidc_int_env", "_int_env"})

_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")

# ---------------------------------------------------------------------------------------------
# Allowlist. `<VAR> -> written reason`, mandatory. Either a config.py read that is
# deliberately NOT a template setting (direction a), or a documented var deliberately not
# read by base-compose/config.py/scripts/frontend (direction b — currently none needed).
# `test_no_stale_allowlist_entries` fails once a listed name stops offending either
# scanner, so this dict can only shrink or turn over, never grow silently.
# ---------------------------------------------------------------------------------------------
_ALLOWLIST: dict[str, str] = {
    "AWS_DEFAULT_REGION": (
        "Standard AWS SDK convention var, read only as BEDROCK_REGION's secondary fallback "
        "behind the already-documented AWS_REGION. Not OpenTranscribe-specific."
    ),
    "DATA_DIR": (
        "Hardcoded container path default (/app/data). No compose service sets it and no "
        "volume exists for another value, so a .env override breaks upload persistence."
    ),
    "DEPLOYMENT_EDITION": (
        "Managed-edition seam value ('community'|'cloud'), set only by the private cloud "
        "build. A self-hosted .env never needs it — see backend/app/core/CLAUDE.md."
    ),
    "ENCRYPTION_ALGORITHM_V3": (
        "Validated at boot against IMPLEMENTED_ENCRYPTION_ALGORITHMS = {'AES-256-GCM'}, the "
        "only algorithm utils/encryption.py's v3 envelope actually implements — setting it to "
        "anything else refuses production startup, and setting it to the only valid value is a "
        "no-op versus the default. Not a real operator knob; see config.py's IMPLEMENTED_"
        "ENCRYPTION_ALGORITHMS comment for why adding a second algorithm here without writing "
        "the decrypt code would silently orphan every existing ciphertext."
    ),
    "MODELS_DIR": (
        "Internal container path default (/app/models). Base compose sets a differently-"
        "named MODELS_DIRECTORY instead, which nothing reads — not wired to any override."
    ),
    "PYANNOTE_MODEL": (
        "Legacy field for a 'pyannote' cloud-ASR provider entry, default mismatched against "
        "the actual call site (services/asr/factory.py uses a separate os.getenv). Not the "
        "diarization model — that's DIARIZATION_MODEL, read by scripts/download-models.py."
    ),
    "TEMP_DIR": (
        "Hardcoded to /app/temp by every service's `environment:` block in base compose — "
        "not sourced from host env, so a .env value would be silently ignored."
    ),
    "TESTING": (
        "Test-harness-only flag (config.py's own pydantic Config class) that decides "
        "whether Settings loads a .env file AT ALL — never itself a .env setting."
    ),
    "USE_GPU": (
        "Dead config.py field: only consumer is the effective_use_gpu property, and nothing "
        "in backend/app calls effective_use_gpu, effective_torch_device, effective_compute_"
        "type, or effective_batch_size (verified by grep) — real hardware detection reads "
        "TORCH_DEVICE/COMPUTE_TYPE/BATCH_SIZE directly via os.getenv in hardware_detection.py "
        "and transcription/config.py instead, bypassing these Settings fields entirely."
    ),
    "WATCH_FOLDER_PATH": (
        "Internal container path, set unconditionally to /watch by docker-compose.watch.yml. "
        "WATCH_HOST_PATH (already documented) is the actual user-facing knob."
    ),
}


def _documented_keys(text: str) -> set[str]:
    """Keys in `.env.example`, including commented `# KEY=value` examples."""
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=", stripped)
        if match:
            keys.add(match.group(1))
    return keys


def _strip_comments(text: str) -> str:
    """Drop `#` comments (full-line and inline), respecting `'...'`/`"..."` quoting."""
    out_lines = []
    for line in text.splitlines():
        out: list[str] = []
        quote: str | None = None
        for char in line:
            if quote is not None:
                out.append(char)
                if char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
                out.append(char)
                continue
            if char == "#":
                break
            out.append(char)
        out_lines.append("".join(out))
    return "\n".join(out_lines)


def _compose_vars(text: str) -> set[str]:
    """`${VAR...}` names docker compose itself interpolates (host/`.env`-side).

    Excludes the doubled-`$` escape (`$${VAR}`) compose uses to pass a literal `$VAR`
    through to a container-side shell (`envsubst '$$NGINX_SERVER_NAME'`, `...@$$HOSTNAME`)
    — resolved INSIDE the container from ITS OWN env, never the host `.env`.
    """
    cleaned = _strip_comments(text)
    names: set[str] = set()
    for match in re.finditer(r"\$\{([A-Z_][A-Z0-9_]*)", cleaned):
        if match.start() > 0 and cleaned[match.start() - 1] == "$":
            continue  # escaped: $${VAR} — container-side, not host `.env`
        names.add(match.group(1))
    return names - _DOCKER_NATIVE


def _is_os_attr(node: ast.expr, attr: str) -> bool:
    """Whether *node* is the attribute chain `os.<attr>` (e.g. `os.environ`, `os.getenv`)."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _literal_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _NAME_RE.fullmatch(node.value) else None
    return None


def _config_py_vars(text: str) -> set[str]:
    """Exact env-var names `config.py` reads, via AST — not `Settings` field names.

    Only three shapes count: `os.getenv("NAME", ...)`, `os.environ.get("NAME", ...)` /
    `os.environ["NAME"]`, and the four `_ENV_HELPER_NAMES`, each with a literal string
    first argument. A `Settings` field NAME is deliberately not a proxy for "the var it
    reads": `MODEL_BASE_DIR: Path = Path(os.getenv("MODELS_DIR", ...))` reads MODELS_DIR,
    not MODEL_BASE_DIR, and many fields (`JWT_ALGORITHM`, `DEBUG`, index-name constants)
    are hardcoded or derived, not independent env inputs at all.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_getenv = _is_os_attr(func, "getenv")
            is_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_attr(func.value, "environ")
            )
            is_helper = isinstance(func, ast.Name) and func.id in _ENV_HELPER_NAMES
            if is_getenv or is_environ_get or is_helper:
                name = _literal_name(node.args[0])
                if name:
                    names.add(name)
        elif isinstance(node, ast.Subscript) and _is_os_attr(node.value, "environ"):
            name = _literal_name(node.slice if isinstance(node.slice, ast.expr) else None)
            if name:
                names.add(name)
    return names


#: The direction-(b) surface. Scoped to real readers only — no docs/*.md, which documents
#: a variable without consuming it.
_SURFACE_GLOBS = (
    "docker-compose*.yml",
    "opentr.sh",
    "scripts/**/*.sh",
    "scripts/**/*.py",
    "nginx/**/*",
    "frontend/*.ts",
    "frontend/src/**/*.ts",
    "frontend/src/**/*.svelte",
    "backend/app/**/*.py",
)


@lru_cache(maxsize=1)
def _broad_surface_names() -> frozenset[str]:
    """Every `[A-Z][A-Z0-9_]{2,}` token anywhere on the deployment surface.

    Plain substring scan on purpose — compose interpolation, a container env mapping, a
    bare shell `$FOO`, and `import.meta.env.VITE_FOO` are all valid consumption and are
    not worth four parsers. Cached: direction (b) calls this once per test, not once per
    file — the inverse of what cost the sibling coverage test 57s in a loop.
    """
    token = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
    seen: set[str] = set()
    for pattern in _SURFACE_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file():
                continue
            try:
                seen.update(token.findall(path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:  # pragma: no cover - defensive
                continue
    return frozenset(seen)


def _reader_names() -> set[str]:
    """The direction-(a) reader set: base compose union config.py, real files."""
    return _compose_vars(BASE_COMPOSE.read_text(encoding="utf-8")) | _config_py_vars(
        CONFIG_PY.read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------------------------
# The assertions this file exists for
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_reader_vars_are_documented_or_allowlisted():
    """(a) Everything base compose or config.py reads must appear in the template.

    A commented `# KEY=value` line counts (module docstring) — fails only on a name
    absent from `.env.example` entirely.
    """
    documented = _documented_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))
    offenders = sorted(_reader_names() - documented - set(_ALLOWLIST))
    assert not offenders, (
        f"{len(offenders)} var(s) read by docker-compose.yml or config.py but absent from "
        f".env.example (commented counts as present): {offenders}\n"
        "Add a (optionally commented) template entry, or an _ALLOWLIST reason."
    )


@pytest.mark.unit
def test_documented_vars_are_consumed_or_allowlisted():
    """(b) Everything in the template must be read by something on the real surface."""
    documented = _documented_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))
    offenders = sorted(documented - _broad_surface_names() - set(_ALLOWLIST))
    assert not offenders, (
        f"{len(offenders)} .env.example var(s) read by nothing on the deployment surface: "
        f"{offenders}\nDelete the dead entry, or add an _ALLOWLIST reason."
    )


@pytest.mark.unit
def test_no_stale_allowlist_entries():
    """An exemption whose offense is gone is an exemption nobody will ever delete."""
    documented = _documented_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))
    offending = (_reader_names() - documented) | (documented - _broad_surface_names())
    stale = sorted(set(_ALLOWLIST) - offending)
    assert not stale, f"{len(stale)} allowlist entry(ies) no longer offend either check: {stale}"


@pytest.mark.unit
def test_allowlist_entries_carry_a_written_reason():
    thin = sorted(key for key, reason in _ALLOWLIST.items() if len(reason.strip()) < 40)
    assert not thin, f"allowlist entries with no substantive reason: {thin}"


# ---------------------------------------------------------------------------------------------
# Guard the guard. Each case is a shape that would silently defeat a scanner above.
# ---------------------------------------------------------------------------------------------

_COMPOSE_CASES: tuple[tuple[str, str, set[str]], ...] = (
    (
        "bare interpolation",
        "environment:\n  - FOO=${SOME_UNDOCUMENTED_VAR}\n",
        {"SOME_UNDOCUMENTED_VAR"},
    ),
    (
        "with a default",
        "ports:\n  - ${SOME_UNDOCUMENTED_VAR:-8080}:80\n",
        {"SOME_UNDOCUMENTED_VAR"},
    ),
    ("docker-native is excluded", "x: ${COMPOSE_PROJECT_NAME:-ot}\n", set()),
    ("full-line comment is not code", "# uses ${SOME_UNDOCUMENTED_VAR}\n", set()),
    ("inline comment is not code", "x: 1  # see ${SOME_UNDOCUMENTED_VAR}\n", set()),
    (
        "quoted # is not a comment start",
        'x: "http://h/#${SOME_UNDOCUMENTED_VAR}"\n',
        {"SOME_UNDOCUMENTED_VAR"},
    ),
    ("escaped for the container shell", "test: sh -c 'ping@$${SOME_UNDOCUMENTED_VAR}'\n", set()),
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "text", "expected"), _COMPOSE_CASES, ids=[c[0] for c in _COMPOSE_CASES]
)
def test_compose_scanner_shapes(label, text, expected):
    assert _compose_vars(text) == expected, label


@pytest.mark.unit
def test_config_py_scanner_must_fire_on_getenv_and_helpers():
    fixture = (
        "import os\n"
        "class Settings:\n"
        "    A: str = os.getenv('SOME_UNDOCUMENTED_A', '')\n"
        "    B: str = os.environ.get('SOME_UNDOCUMENTED_B', '')\n"
        "    C: int = _int_env('SOME_UNDOCUMENTED_C', 1)\n"
        "    D: str = oidc_env('SOME_UNDOCUMENTED_D')\n"
        "    E: str | None = (\n"
        "        int(os.environ['SOME_UNDOCUMENTED_E'])\n"
        "        if os.environ.get('SOME_UNDOCUMENTED_E') else None\n"
        "    )\n"
    )
    found = _config_py_vars(fixture)
    for name in ("A", "B", "C", "D", "E"):
        assert f"SOME_UNDOCUMENTED_{name}" in found, f"missed {name}-shaped read"


@pytest.mark.unit
def test_config_py_scanner_ignores_field_names_and_unrelated_calls():
    """The false positive this scanner exists to avoid: a field name is not its env var."""
    fixture = (
        "import os\n"
        "class Settings:\n"
        "    MODEL_BASE_DIR: Path = Path(os.getenv('MODELS_DIR', '/app/models'))\n"
        "    JWT_ALGORITHM: str = 'HS256'\n"
        "    DEBUG: bool = is_relaxed(ENVIRONMENT)\n"
        "    d = {'X': 1}\n"
        "    d.get('SHOULD_NOT_APPEAR')\n"
    )
    assert _config_py_vars(fixture) == {"MODELS_DIR"}


@pytest.mark.unit
def test_documented_keys_parser_treats_commented_lines_as_documented():
    """The exact distinction the issue's spot-check got wrong (module docstring)."""
    fixture = "# GPU_MAX_TASKS=100000\nLIVE_VAR=1\n# comment with no assignment shape\n"
    assert _documented_keys(fixture) == {"GPU_MAX_TASKS", "LIVE_VAR"}


@pytest.mark.unit
def test_consumption_scanner_finds_names_and_the_corpus_is_real():
    """Must-fire for (b), plus a corpus check so a glob typo can't zero out the surface."""
    surface = _broad_surface_names()
    assert "SOME_NAME_NOTHING_DEFINES_ANYWHERE_AT_ALL" not in surface
    assert "POSTGRES_HOST" in surface  # trusted: read by both base compose and config.py
    assert len(surface) > 500, f"surface scan found suspiciously few names: {len(surface)}"


@pytest.mark.unit
def test_reader_and_documented_scans_are_nonempty_on_the_real_repo():
    """Guard against a path typo silently making every check above pass vacuously.

    The floor is a sanity check that the scan isn't vacuous, not a target count: it
    was 200 back when .env.example was ~1900 lines. `8ca346a2` and later commits cut
    it to secrets/system-config only (currently ~165-170 documented keys) — most of
    what it used to list is now UI-configurable `SystemSettings`, not `.env`. Lower
    this again only if a future trim drops the real count below it for the same
    legitimate reason; don't raise it back to chase a stale historical number.
    """
    assert len(_reader_names()) > 50
    assert len(_documented_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))) > 100
