"""Every ``_int_env``/``_float_env``-style Settings field must be a ``ClassVar``.

Commit 5f9f2ffd converted ~60 fields in ``app/core/config.py`` from plain pydantic-settings
fields to ``ClassVar[...]``, because pydantic-settings re-sources and re-validates every
*declared* (non-ClassVar) field from its environment variable at ``Settings()`` construction —
independent of, and after, any class-body Python expression that computed a "safe" fallback.
A malformed env value (e.g. an inline ``# comment`` docker compose's ``env_file`` loading does
not strip) crashed the whole backend at import time even though ``_int_env``/``_float_env``
looked like they guarded exactly that case.

Nothing currently stops a future contributor from reusing the ``_int_env``/``_float_env``/
``oidc_int_env`` pattern (still the established pattern — used ~60+ times) while forgetting
``ClassVar[...]``, reintroducing the exact crash for that one field, invisibly. This module is
that guard, following the ``test_ddl_marker_discipline.py`` shape: a static AST scan over the
real source, plus must-fire/must-stay-clean cases proving the scan is not blind (a detector
that matches nothing is indistinguishable from a clean file — issue #431).

Two independent shapes need the guard, because ``config.py`` uses two independent patterns to
get a "safe fallback that only works with ClassVar":

1. A class-body assignment whose value is a direct call to ``_int_env``/``_float_env``/
   ``oidc_int_env`` (~60 fields, e.g. ``JWT_ACCESS_TOKEN_EXPIRE_MINUTES``).
2. A manual try/except fallback block assigning a class-level name with no default-taking
   helper call (``NUM_SPEAKERS``) — these already declare the name via a bare
   ``NAME: ClassVar[...]`` ``AnnAssign`` ahead of the ``if``/``try`` block that actually sets
   it, and the check below requires that declaration to exist and be a ClassVar.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "app" / "core" / "config.py"

#: Helper calls whose whole purpose is a fallback default that pydantic-settings bypasses
#: unless the field is a ClassVar. ``oidc_bool_env`` is deliberately excluded: a malformed
#: bool env var cannot raise (``"..."`` .lower() == "true" is always a valid bool), so it
#: carries none of the crash risk this guard exists for.
_FALLBACK_HELPER_NAMES = frozenset({"_int_env", "_float_env", "oidc_int_env"})


def _call_name(node: ast.AST) -> str | None:
    """Best-effort function name of a ``Call`` node, e.g. ``_int_env`` or ``oidc_int_env``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_classvar_annotation(annotation: ast.expr) -> bool:
    """True for ``ClassVar[...]`` or bare ``ClassVar`` (both appear in config.py)."""
    target = annotation
    if isinstance(target, ast.Subscript):
        target = target.value
    return isinstance(target, ast.Name) and target.id == "ClassVar"


def _settings_class_body(tree: ast.Module) -> list[ast.stmt]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return node.body
    raise AssertionError("no `class Settings` found in the scanned source")


def _find_fallback_helper_findings(tree: ast.Module) -> list[str]:
    """Assignments whose value directly calls a fallback helper but lack ``ClassVar``.

    Covers both ``AnnAssign`` (``NAME: int = _int_env(...)``) and plain ``Assign``
    (``NAME = _int_env(...)``, which is legal Python and just as dangerous — the absence of
    an annotation does not exempt a field from pydantic-settings re-sourcing it).
    """
    findings: list[str] = []
    for node in _settings_class_body(tree):
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            name = node.target.id if isinstance(node.target, ast.Name) else "<?>"
            helper = _call_name(node.value)
            if helper in _FALLBACK_HELPER_NAMES and not _is_classvar_annotation(node.annotation):
                findings.append(name)
        elif isinstance(node, ast.Assign):
            helper = _call_name(node.value)
            if helper in _FALLBACK_HELPER_NAMES:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        findings.append(target.id)
    return findings


#: Node types that introduce a new scope. A ``try``/``except`` inside one of these (e.g. the
#: ``try: from app.utils.hardware_detection import detect_hardware`` blocks inside
#: ``effective_use_gpu`` and its siblings) assigns a plain *local* variable, not a Settings
#: field, and must not be walked into.
_SCOPE_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _iter_class_scope_try_nodes(body: list[ast.stmt]) -> list[ast.Try]:
    """Every ``Try`` node reachable from the class body without crossing a scope boundary.

    Control-flow wrappers (``if``/``else``, nested ``try``) stay in scope — that's exactly
    the ``NUM_SPEAKERS`` shape (``if _NUM_SPEAKERS_STR: try: ... except: ...``) — but a
    ``def``/nested ``class`` is a new scope and is never descended into.
    """
    found: list[ast.Try] = []

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARY):
                continue
            if isinstance(child, ast.Try):
                found.append(child)
            _walk(child)

    for stmt in body:
        if isinstance(stmt, _SCOPE_BOUNDARY):
            continue
        if isinstance(stmt, ast.Try):
            found.append(stmt)
        _walk(stmt)
    return found


def _find_manual_fallback_findings(tree: ast.Module) -> list[str]:
    """A class-level name reassigned inside a ``try``/``except`` (the ``NUM_SPEAKERS`` shape)
    with no preceding ``NAME: ClassVar[...]`` declaration in the same class body.

    ``NUM_SPEAKERS`` declares ``NUM_SPEAKERS: ClassVar[int | None]`` as a bare (valueless)
    ``AnnAssign`` immediately before the ``if``/``try`` block that actually assigns it. A
    future field using this same manual-fallback shape but skipping that declaration would
    be re-sourced by pydantic-settings straight past the try/except, exactly like the bug
    5f9f2ffd fixed.
    """
    body = _settings_class_body(tree)

    classvar_declared: set[str] = set()
    for node in body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and _is_classvar_annotation(node.annotation)
        ):
            classvar_declared.add(node.target.id)

    findings: set[str] = set()
    for try_node in _iter_class_scope_try_nodes(body):
        for stmt in ast.walk(try_node):
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id not in classvar_declared:
                    findings.add(target.id)
    return sorted(findings)


def _scan(source: str) -> tuple[list[str], list[str]]:
    """Return (helper_call_findings, manual_fallback_findings) for one module's source."""
    tree = ast.parse(textwrap.dedent(source))
    return _find_fallback_helper_findings(tree), _find_manual_fallback_findings(tree)


# --------------------------------------------------------------------------------------
# The real gate: config.py must currently be clean.
# --------------------------------------------------------------------------------------


def test_every_fallback_helper_field_is_a_classvar() -> None:
    """A field assigned via ``_int_env``/``_float_env``/``oidc_int_env`` without ``ClassVar``
    is re-sourced by pydantic-settings from its raw env var at ``Settings()`` construction,
    bypassing the helper's try/except entirely — the exact crash 5f9f2ffd fixed.
    """
    helper_findings, _ = _scan(_CONFIG_PATH.read_text())
    assert not helper_findings, (
        "These Settings fields call _int_env/_float_env/oidc_int_env but are not declared "
        "ClassVar, so pydantic-settings re-validates them from the raw env var and can crash "
        "backend startup on a malformed value (see 5f9f2ffd): " + ", ".join(sorted(helper_findings))
    )


def test_every_manual_fallback_field_is_a_classvar() -> None:
    """The NUM_SPEAKERS-shaped manual try/except fallback needs the same ClassVar guard."""
    _, manual_findings = _scan(_CONFIG_PATH.read_text())
    assert not manual_findings, (
        "These Settings fields are assigned inside a try/except fallback block (the "
        "NUM_SPEAKERS shape) but have no preceding `NAME: ClassVar[...]` declaration, so "
        "pydantic-settings can still re-source and crash on them: "
        + ", ".join(sorted(manual_findings))
    )


# --------------------------------------------------------------------------------------
# Guard the guard: must-fire and must-stay-clean cases, so a scanner matching nothing
# cannot be mistaken for a clean file (issue #431).
# --------------------------------------------------------------------------------------


def test_must_fire_helper_call_without_classvar() -> None:
    """A field using _int_env with a plain `int` annotation must be flagged."""
    source = """
        class Settings:
            BAD_FIELD: int = _int_env("BAD_FIELD", 5)
    """
    helper_findings, manual_findings = _scan(source)
    assert helper_findings == ["BAD_FIELD"]
    assert manual_findings == []


def test_must_fire_helper_call_with_no_annotation_at_all() -> None:
    """Even with no annotation, a direct helper-call assignment is still re-sourced."""
    source = """
        class Settings:
            BAD_FIELD = _int_env("BAD_FIELD", 5)
    """
    helper_findings, _ = _scan(source)
    assert helper_findings == ["BAD_FIELD"]


def test_must_fire_oidc_int_env_without_classvar() -> None:
    source = """
        class Settings:
            BAD_TIMEOUT: int = oidc_int_env("BAD_TIMEOUT", 30)
    """
    helper_findings, _ = _scan(source)
    assert helper_findings == ["BAD_TIMEOUT"]


def test_must_fire_manual_fallback_without_classvar_declaration() -> None:
    """The NUM_SPEAKERS shape, but with the ClassVar declaration forgotten."""
    source = """
        class Settings:
            _BAD_STR: str | None = os.getenv("BAD_FIELD")
            try:
                BAD_FIELD = int(_BAD_STR)
            except ValueError:
                BAD_FIELD = None
    """
    _, manual_findings = _scan(source)
    assert manual_findings == ["BAD_FIELD"]


def test_must_stay_clean_helper_call_with_classvar() -> None:
    source = """
        class Settings:
            GOOD_FIELD: ClassVar[int] = _int_env("GOOD_FIELD", 5)
    """
    helper_findings, manual_findings = _scan(source)
    assert helper_findings == []
    assert manual_findings == []


def test_must_stay_clean_oidc_int_env_with_classvar() -> None:
    source = """
        class Settings:
            GOOD_TIMEOUT: ClassVar[int] = oidc_int_env("GOOD_TIMEOUT", 30)
    """
    helper_findings, _ = _scan(source)
    assert helper_findings == []


def test_must_stay_clean_manual_fallback_with_classvar_declaration() -> None:
    """The real NUM_SPEAKERS shape: declared ClassVar first, assigned in try/except after."""
    source = """
        class Settings:
            _GOOD_STR: str | None = os.getenv("GOOD_FIELD")
            GOOD_FIELD: ClassVar[int | None]
            if _GOOD_STR:
                try:
                    GOOD_FIELD = int(_GOOD_STR)
                except ValueError:
                    GOOD_FIELD = None
            else:
                GOOD_FIELD = None
    """
    _, manual_findings = _scan(source)
    assert manual_findings == []


def test_must_stay_clean_unrelated_field_shapes() -> None:
    """Ordinary plain fields (LDAP_SERVER-style os.getenv() calls, bools, plain literals) are
    not helper-fallback fields at all and must never be flagged — the guard is scoped to the
    fallback-helper pattern, not "every Settings field needs ClassVar".
    """
    source = """
        class Settings:
            LDAP_SERVER: str = os.getenv("LDAP_SERVER", "")
            LDAP_USE_SSL: bool = os.getenv("LDAP_USE_SSL", "true").lower() == "true"
            SOME_CONSTANT: int = 42
    """
    helper_findings, manual_findings = _scan(source)
    assert helper_findings == []
    assert manual_findings == []


# --------------------------------------------------------------------------------------
# Live end-to-end regression: prove the fix holds through real Settings() construction,
# not just that the AST pattern is present.
# --------------------------------------------------------------------------------------


def test_malformed_env_value_does_not_crash_settings_construction() -> None:
    """A trailing-comment-corrupted env value for a ClassVar-protected int field must not
    raise when ``Settings()`` is constructed — reproducing the exact `.env.example` bug
    shape from 5f9f2ffd (docker compose's `env_file` loading does not strip an inline `#
    comment`, so `NUM_SPEAKERS=20  # force an exact count` becomes the literal value
    `"20  # force an exact count"`).

    Run in a subprocess so the malformed env var cannot leak into (or be polluted by) the
    rest of this process's environment or module-import cache.
    """
    script = """
import os
os.environ["NUM_SPEAKERS"] = "20  # force an exact speaker count for this recording"
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "transcribe_test")
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("DATA_DIR", "/tmp/ot-classvar-test/data")
os.environ.setdefault("MODELS_DIR", "/tmp/ot-classvar-test/models")
os.environ.setdefault("TEMP_DIR", "/tmp/ot-classvar-test/temp")
from app.core.config import Settings

settings = Settings()
assert settings.NUM_SPEAKERS is None, (
    f"expected the malformed value to fall back to None, got {settings.NUM_SPEAKERS!r}"
)
print("OK")
"""
    backend_root = _CONFIG_PATH.parents[2]
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "Settings() crashed on a malformed NUM_SPEAKERS value instead of falling back "
        f"(the exact bug 5f9f2ffd fixed):\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
