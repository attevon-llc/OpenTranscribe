"""Structural contract (W2.0g fix #6): every ``get_accessible_file_ids_subquery``
call site in ``backend/app/services/chat/`` must thread ``organization_id``.

This is the pattern the org-gate leak (fix #2, ``aggregation_service.py``) was
one instance of: ``organization_id`` is keyword-only and defaults to the
``UNSCOPED`` sentinel — no tenant gate at all, not "personal scope" — so an
omitted keyword is silent, type-checks, and only shows up as a caller's own
org-stamped files counting as if they were org-less. A functional test proves
one call site; this proves the *shape of the rule* holds for every call site in
the package, including ones added after this pass.

An AST scan, not a grep, because ``organization_id`` can appear as a substring
of a comment or docstring quoting the very rule being enforced (this file's
own module docstring is a live example) — a regex match would either miss the
real call or false-positive on prose.

**Extended (Finding #9, the quarantine-gap review) to
``_accessible_scoped_files`` call sites too.** That helper is
``aggregation_service.py``'s single Postgres-side chokepoint — it wraps
``get_accessible_file_ids_subquery`` AND now also excludes quarantined files
(the fix for Finding #1) — so a caller writing
``_accessible_scoped_files(db, user_id, None, file_uuids)``, or threading the
``UNSCOPED`` sentinel through by hand, would reopen exactly the org-gate hole
this file exists to close, one indirection layer down from the original bug.
Unlike the outer function, ``organization_id`` there is an ordinary
**positional** parameter with no keyword-only enforcement at all, so a
hardcoded literal is syntactically legal in a way the outer call site's
keyword-only signature does not allow — this detector is what catches it
instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CHAT_PACKAGE = Path(__file__).resolve().parents[2] / "app" / "services" / "chat"

_TARGET_NAME = "get_accessible_file_ids_subquery"


def find_missing_organization_id(source: str) -> list[int]:
    """Line numbers of ``get_accessible_file_ids_subquery`` calls with no
    ``organization_id`` keyword argument.

    ``organization_id`` is keyword-only in the real signature, so a call that
    supplies it can only do so as a keyword — a positional-arg false negative
    is not possible here. A ``**kwargs`` unpack (``kw.arg is None``) is treated
    as satisfying the contract: it cannot be verified statically, and flagging
    it would be a false finding rather than a real one.
    """
    tree = ast.parse(source)
    violations: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name != _TARGET_NAME:
            continue
        threaded = any(kw.arg == "organization_id" or kw.arg is None for kw in node.keywords)
        if not threaded:
            violations.append(node.lineno)
    return violations


def test_every_chat_package_call_site_threads_organization_id():
    """The real gate: scans every module actually shipped in the package."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(_CHAT_PACKAGE.glob("*.py")):
        lines = find_missing_organization_id(path.read_text())
        if lines:
            offenders[path.name] = lines

    assert not offenders, (
        f"{_TARGET_NAME}() called with no organization_id (defaults to UNSCOPED — "
        f"no tenant gate at all, not personal scope) in: {offenders}"
    )


# ---------------------------------------------------------------------------
# Finding #9 — the same rule, one indirection layer down:
# ``_accessible_scoped_files`` call sites must never pass a hardcoded
# ``None``/``UNSCOPED`` literal for ``organization_id``.
# ---------------------------------------------------------------------------

_SCOPED_FILES_TARGET = "_accessible_scoped_files"
#: ``_accessible_scoped_files(db, user_id, organization_id, file_uuids)`` —
#: organization_id is the 3rd argument, ordinary positional (no keyword-only
#: enforcement the way the outer ``get_accessible_file_ids_subquery`` has).
_SCOPED_FILES_ORG_ID_INDEX = 2


def _is_hardcoded_literal(node: ast.expr) -> bool:
    """``None`` or the bare name ``UNSCOPED`` — a value no real caller derives
    from its own request context."""
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.Name) and node.id == "UNSCOPED"


def find_hardcoded_organization_id_on_scoped_files(source: str) -> list[int]:
    """Line numbers of ``_accessible_scoped_files`` calls whose
    ``organization_id`` argument — positional index 2, or the keyword — is a
    hardcoded ``None``/``UNSCOPED`` literal rather than threaded from the
    caller's own context.
    """
    tree = ast.parse(source)
    violations: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name != _SCOPED_FILES_TARGET:
            continue
        positional_hit = len(node.args) > _SCOPED_FILES_ORG_ID_INDEX and _is_hardcoded_literal(
            node.args[_SCOPED_FILES_ORG_ID_INDEX]
        )
        keyword_hit = any(
            kw.arg == "organization_id" and _is_hardcoded_literal(kw.value) for kw in node.keywords
        )
        if positional_hit or keyword_hit:
            violations.append(node.lineno)
    return violations


def test_every_scoped_files_call_site_avoids_a_hardcoded_organization_id():
    """The real gate for the extended rule."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(_CHAT_PACKAGE.glob("*.py")):
        lines = find_hardcoded_organization_id_on_scoped_files(path.read_text())
        if lines:
            offenders[path.name] = lines

    assert not offenders, (
        f"{_SCOPED_FILES_TARGET}() called with a hardcoded organization_id literal "
        f"(None/UNSCOPED — no tenant gate at all) in: {offenders}"
    )


def test_scoped_files_detector_fires_on_a_positional_none():
    """Must-fire: the exact shape a careless future call site would have —
    positional, no keyword, easy to write by accident."""
    broken = (
        "def f(db, user_id, file_uuids):\n"
        "    return _accessible_scoped_files(db, user_id, None, file_uuids)\n"
    )

    assert find_hardcoded_organization_id_on_scoped_files(broken) == [2]


def test_scoped_files_detector_fires_on_a_keyword_unscoped():
    """Must-fire: the keyword form, with the sentinel spelled out by name."""
    broken = (
        "def f(db, user_id, file_uuids):\n"
        "    return _accessible_scoped_files(\n"
        "        db, user_id, file_uuids=file_uuids, organization_id=UNSCOPED\n"
        "    )\n"
    )

    assert find_hardcoded_organization_id_on_scoped_files(broken) == [2]


def test_scoped_files_detector_stays_clean_on_a_threaded_variable():
    """Must-stay-clean: both of the module's real call sites — a variable of
    the caller's own, threaded through positionally."""
    clean = (
        "def _occurrence_count(db, subject, user_id, organization_id, file_uuids):\n"
        "    scoped_predicate = _accessible_scoped_files(\n"
        "        db, user_id, organization_id, file_uuids\n"
        "    )\n"
        "    return scoped_predicate\n"
    )

    assert find_hardcoded_organization_id_on_scoped_files(clean) == []


def test_scoped_files_detector_does_not_fire_on_an_unrelated_none_argument():
    """Must-stay-clean: a `None` passed to a DIFFERENT positional slot, or to
    a different function entirely, must not be flagged."""
    clean = (
        "def f(db, user_id, organization_id, file_uuids):\n"
        "    return _accessible_scoped_files(db, user_id, organization_id, None)\n"
        "\n"
        "def g(db, user_id):\n"
        "    return some_other_function(db, user_id, None)\n"
    )

    assert find_hardcoded_organization_id_on_scoped_files(clean) == []


def test_detector_fires_on_a_deliberately_broken_call():
    """Must-fire case: this is the exact shape the fixed bug had."""
    broken = (
        "def _occurrence_count(db, subject, user_id, file_uuids):\n"
        "    accessible = PermissionService.get_accessible_file_ids_subquery(db, user_id)\n"
        "    return accessible\n"
    )

    assert find_missing_organization_id(broken) == [2]


def test_detector_stays_clean_on_a_correctly_threaded_call():
    """Must-stay-clean case: a detector that fires on everything is as useless
    as one that fires on nothing."""
    clean = (
        "def _occurrence_count(db, subject, user_id, organization_id, file_uuids):\n"
        "    accessible = PermissionService.get_accessible_file_ids_subquery(\n"
        "        db, user_id, organization_id=organization_id\n"
        "    )\n"
        "    return accessible\n"
    )

    assert find_missing_organization_id(clean) == []


def test_detector_is_not_fooled_by_a_bare_name_import():
    """`from ... import get_accessible_file_ids_subquery as fn; fn(db, user_id)`
    is the same call under a different spelling and must be caught too."""
    broken = "def f(db, user_id):\n    return get_accessible_file_ids_subquery(db, user_id)\n"

    assert find_missing_organization_id(broken) == [2]


def test_detector_accepts_a_kwargs_unpack_it_cannot_verify_statically():
    """A `**kwargs` forward is not a false finding — it cannot be checked
    without evaluating the call, which an AST scan does not do."""
    forwarded = (
        "def f(db, user_id, **kwargs):\n"
        "    return get_accessible_file_ids_subquery(db, user_id, **kwargs)\n"
    )

    assert find_missing_organization_id(forwarded) == []
