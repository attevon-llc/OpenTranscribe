"""Fast, DB-free safety proofs for ``scripts/cleanup-test-users.py`` (issue #601).

The script had ZERO test coverage — 14 hits for its name across the whole test tree, every
one prose (docstrings/comments). Nothing imports it, invokes it, or statically inspects it.
Its only caller is ``scripts/run-integration-tests.sh``'s dry-run cleanup phase, wrapped in
``|| true``, which swallows a crash.

Investigating the "zero coverage" framing surfaced three real bugs, fixed alongside these
tests:

* **Bug A.** The script built its DSN from ``POSTGRES_TEST_PORT`` — a name that appears
  NOWHERE ELSE in the repo, not in ``.env.example``, not in ``docker-compose.yml``. It always
  silently fell back to ``5176``, the LIVE dev stack's port, regardless of what an operator
  intended to target (e.g. an isolated ``--fresh --port-offset 100`` stack on 5276). Fixed by
  reading ``POSTGRES_PORT`` — the variable the rest of the repo actually uses — and printing
  the resolved target as the first line of every run.
* **Bug B.** The bulk ``DELETE FROM "user" WHERE id = ANY(:ids)`` was one statement for the
  whole batch. Any candidate holding a leftover ``tag``/``task``/``comment``/``collection``/
  ``speaker``/``speaker_profile``/``speaker_collection`` row (all ``ON DELETE NO ACTION`` into
  ``user`` — see ``test_user_deletion_fk_coverage.py``) made the WHOLE statement raise
  ``IntegrityError``, deleting nobody and crashing before the LLM-config sweep ever ran.
* **Bug C.** The dry-run report printed ``WOULD DELETE`` for rows Bug B could never actually
  remove, with no indication of the risk.

This file is unit-only (no DB, no Docker, no live stack) — the integration proof against a
throwaway Postgres lives in
``backend/tests/integration/test_cleanup_test_users_isolated_db.py``.

Loading pattern follows ``test_audit_tests_selftest.py``/``test_session_lifetime_audit.py``:
``importlib.util.spec_from_file_location``, because the script's hyphenated filename blocks a
normal import. Importing it calls ``dotenv_values(<repo>/.env)`` — a harmless read (opens no
socket) — but its return value is never printed here, in line with the repo's rule against
touching ``.env`` contents.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "cleanup-test-users.py"
_E2E_CONFTEST = _REPO_ROOT / "backend" / "tests" / "e2e" / "conftest.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cleanup_test_users", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_script()


# ---------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------


def _like_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a SQL ``LIKE ... ESCAPE '\\'`` pattern into a regex.

    ``%`` -> any run of characters. An ESCAPED underscore (``\\_``) is a LITERAL
    underscore — every pattern in ``ORPHAN_PATTERNS`` escapes its underscores on
    purpose (see ``test_the_escaped_underscore_is_not_a_wildcard``), so a translator
    that instead treated ``\\_`` as a wildcard would make the prefix-coverage check
    (test #4) pass vacuously: a wildcard swallows anything, so it can never prove a
    real prefix was matched. An UNESCAPED ``_`` keeps SQL LIKE's normal meaning
    (any one character) for completeness, though nothing here currently relies on it.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif ch == "%":
            out.append(".*")
            i += 1
        elif ch == "_":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _email_minting_prefixes(source: str) -> dict[str, str]:
    """``{CONSTANT_NAME: prefix_value}`` for module-level constants that mint an email.

    A constant only counts if the same file builds an ``...@example...`` f-string out of
    it (``f"{PREFIX}{uuid...}@example.com"``) — this is what separates
    ``SECOND_USER_PREFIX`` (an email prefix) from ``OWNED_MEDIA_PREFIX`` (a filename
    prefix) and ``SHARED_COLLECTION_PREFIX`` (a collection-name prefix): none of the
    latter two belong in ``ORPHAN_PATTERNS``, which matches only ``user.email``.
    """
    prefixes: dict[str, str] = {}
    for match in re.finditer(r'^([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*-)"\s*$', source, re.MULTILINE):
        name, value = match.group(1), match.group(2)
        usage = re.compile(r'f"\{' + re.escape(name) + r'\}[^"]*@example')
        if usage.search(source):
            prefixes[name] = value
    return prefixes


def _find_ungated_deletes(source: str) -> list[str]:
    """Every ``'DELETE FROM ...'`` string constant NOT inside an execute-truthy branch.

    A "guard" is an ``if`` whose test is bare ``execute`` or ``<something>.execute``
    (matches both ``if execute:`` and ``if args.execute:``). ``if not execute:`` does
    NOT count as a guard for its ``body`` — that is the dry-run branch, the one place a
    literal DELETE would be the actual bug. Only the module scope is required to have
    the guard somewhere in the literal's ancestor chain; it does not need to be in the
    same function as the ``if`` (a `with` inside an `if` inside a function all still
    count, since the visitor descends through every node type).
    """

    def _is_execute_guard(test: ast.expr) -> bool:
        if isinstance(test, ast.Name) and test.id == "execute":
            return True
        return isinstance(test, ast.Attribute) and test.attr == "execute"

    tree = ast.parse(source)
    violations: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.guard_depth = 0

        def visit_If(self, node: ast.If) -> None:  # noqa: N802 - ast.NodeVisitor convention
            if _is_execute_guard(node.test):
                self.guard_depth += 1
                for stmt in node.body:
                    self.visit(stmt)
                self.guard_depth -= 1
                for stmt in node.orelse:
                    self.visit(stmt)
            else:
                self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
            if (
                isinstance(node.value, str)
                and "DELETE FROM" in node.value
                and self.guard_depth == 0
            ):
                violations.append(f"line {node.lineno}: {node.value!r}")

    _Visitor().visit(tree)
    return violations


class _FakeRow(NamedTuple):
    """Standalone stand-in for ``cleanup.UserRow`` — proves ``classify`` needs only
    duck-typed ``.id``/``.email``/``.files`` attributes, not the concrete class."""

    id: int
    email: str
    files: int


# ---------------------------------------------------------------------------------------------
# 1. --execute defaults off
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_execute_defaults_to_false() -> None:
    args = cleanup.build_parser().parse_args([])
    assert args.execute is False


# ---------------------------------------------------------------------------------------------
# 2. The DELETE is reachable only behind the --execute gate
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_delete_is_reachable_only_behind_the_execute_gate() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    violations = _find_ungated_deletes(source)
    assert violations == [], f"DELETE FROM statement(s) not gated by execute: {violations}"


@pytest.mark.unit
def test_the_ungated_delete_detector_actually_fires() -> None:
    """Guard the guard: a synthetic DELETE OUTSIDE any execute-guard must be flagged.

    Without this, the check above could pass because the detector silently matches
    nothing — indistinguishable from a genuinely clean script.
    """
    unguarded_source = (
        "def f(execute):\n"
        "    conn.execute(text('DELETE FROM \"user\" WHERE id = :id'), {'id': 1})\n"
        "    if execute:\n"
        "        pass\n"
    )
    violations = _find_ungated_deletes(unguarded_source)
    assert violations != [], "detector failed to flag a DELETE with no execute guard at all"

    guarded_source = (
        "def f(execute):\n"
        "    if execute:\n"
        "        conn.execute(text('DELETE FROM \"user\" WHERE id = :id'), {'id': 1})\n"
    )
    assert _find_ungated_deletes(guarded_source) == []


# ---------------------------------------------------------------------------------------------
# 3. The port variable is one the repo actually sets (Bug A)
# ---------------------------------------------------------------------------------------------


def _extract_port_setting_names(source: str) -> set[str]:
    return set(re.findall(r"_setting\(\s*['\"]([A-Z_]*PORT[A-Z_]*)['\"]", source))


@pytest.mark.unit
def test_the_target_port_variable_is_one_the_repo_actually_sets() -> None:
    """RED before Bug A's fix: the old script read ``POSTGRES_TEST_PORT``, a name that
    appears in neither ``.env.example`` nor ``docker-compose.yml`` — an orphan that always
    silently resolved to the hardcoded ``5176`` fallback. GREEN after: it reads
    ``POSTGRES_PORT``, the variable ``opentr.sh``'s own ``--port-offset`` machinery moves.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    port_vars = _extract_port_setting_names(source)
    assert port_vars, "no _setting(...) call reading a *PORT*-shaped name found"

    env_example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    orphans = {v for v in port_vars if v not in env_example and v not in compose}
    assert orphans == set(), (
        f"port variable(s) {orphans} appear in neither .env.example nor docker-compose.yml "
        "— nothing in the repo actually sets them, so they always resolve to the hardcoded "
        "fallback regardless of what a caller intended to target"
    )


# ---------------------------------------------------------------------------------------------
# 4. Every e2e-minted email prefix has a matching ORPHAN_PATTERNS entry
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_e2e_registered_prefix_has_an_orphan_pattern() -> None:
    """Guards the ``mfa-e2e-`` class of regression: a prefix minted by the e2e suite with
    no matching cleanup pattern, invisible until a run dies mid-flight and leaves an
    account no sweep can find.
    """
    source = _E2E_CONFTEST.read_text(encoding="utf-8")
    prefixes = _email_minting_prefixes(source)
    assert "SECOND_USER_PREFIX" in prefixes, (
        "the scan found no email-minting prefix constants in e2e/conftest.py — it may "
        "have stopped matching rather than the file having none"
    )

    regexes = [_like_to_regex(p) for p in cleanup.ORPHAN_PATTERNS]
    unmatched = {
        name: value
        for name, value in prefixes.items()
        if not any(regex.match(f"{value}sample@example.com") for regex in regexes)
    }
    assert unmatched == {}, f"e2e prefixes with no matching ORPHAN_PATTERNS entry: {unmatched}"


# ---------------------------------------------------------------------------------------------
# 5. Keep-list beats even a drifted pattern
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_keep_list_beats_a_drifted_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """KEEP_EMAILS must win even if a future ORPHAN_PATTERNS entry accidentally matches a
    real dev account — the shipped patterns don't reach it today (Observation D /
    ``test_keep_list_is_currently_unreachable_by_the_shipped_patterns``), so this drifts
    the pattern set on purpose to prove the keep-list is the thing actually doing the
    protecting, not a coincidence of the current pattern shapes.
    """
    monkeypatch.setattr(
        cleanup, "ORPHAN_PATTERNS", [*cleanup.ORPHAN_PATTERNS, "admin%@example.com"]
    )
    row = _FakeRow(id=1, email="admin@example.com", files=0)
    kept, _owners, candidates = cleanup.classify([row])
    assert "admin@example.com" in kept
    assert "admin@example.com" not in [email for _id, email in candidates]


@pytest.mark.unit
def test_keep_prefix_beats_a_drifted_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twin of the above for KEEP_PREFIXES (``ldap-``/``kc-``/``superdave``)."""
    monkeypatch.setattr(
        cleanup, "ORPHAN_PATTERNS", [*cleanup.ORPHAN_PATTERNS, "ldap-%@example.com"]
    )
    row = _FakeRow(id=2, email="ldap-user@example.com", files=0)
    kept, _owners, candidates = cleanup.classify([row])
    assert "ldap-user@example.com" in kept
    assert "ldap-user@example.com" not in [email for _id, email in candidates]


# ---------------------------------------------------------------------------------------------
# 6. A media-file owner is never a deletion candidate
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_user_who_owns_media_files_is_never_a_candidate() -> None:
    row = _FakeRow(id=3, email="searchqual-x@example.invalid", files=3)
    kept, owners, candidates = cleanup.classify([row])
    assert kept == []
    assert owners == [("searchqual-x@example.invalid", 3)]
    assert candidates == []


# ---------------------------------------------------------------------------------------------
# 7. The escaped underscore is a literal, not a wildcard (guard the guard for test #4)
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_escaped_underscore_is_not_a_wildcard() -> None:
    pattern = r"testuser\_%@example.com"
    regex = _like_to_regex(pattern)
    assert regex.match("testuser_1@example.com"), "the escaped underscore must match literally"
    assert not regex.match("testuserX1@example.com"), (
        "a translator that treats \\_ as a wildcard would match this too, making the "
        "prefix-coverage check (test #4) pass vacuously"
    )


# ---------------------------------------------------------------------------------------------
# 8. Observation D, recorded as a measured fact
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_keep_list_is_currently_unreachable_by_the_shipped_patterns() -> None:
    """KEEP_EMAILS/KEEP_PREFIXES are defense-in-depth today: no shipped ORPHAN_PATTERNS
    entry actually matches any of them (no pattern matches ``admin@example.com`` or
    ``ldap-*``). That is fine — but it means the keep-list's protection has never been
    *exercised* by the real pattern set, only by the synthetic drift in tests #5/#5b
    above. Recording it here means a future pattern change that starts matching a
    keep-listed value shows up as a red test, not a silent behavior change.
    """
    regexes = [_like_to_regex(p) for p in cleanup.ORPHAN_PATTERNS]
    keep_candidates = set(cleanup.KEEP_EMAILS) | {
        f"{prefix}sample@example.com" for prefix in cleanup.KEEP_PREFIXES
    }
    matched = {email for email in keep_candidates if any(r.match(email) for r in regexes)}
    assert matched == set(), (
        f"a shipped ORPHAN_PATTERNS entry now matches a keep-listed value: {sorted(matched)} "
        "— Observation D no longer holds, verify this is intentional before merging"
    )
