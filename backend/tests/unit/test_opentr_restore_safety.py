"""Static shape checks for the ``./opentr.sh restore`` fix (issue #599).

``./opentr.sh restore`` used to replay a plain ``pg_dump`` file into an already-populated
database with a bare ``psql < backup.sql``. Every statement in the dump failed
(``relation already exists`` / duplicate keys) — but without ``ON_ERROR_STOP``, ``psql``
exits **0** anyway, and the restore reported success while changing nothing. Worse: the
dump's ``alembic_version`` row does not collide on primary key with a drifted row already
present, so it inserts successfully while every data-table ``COPY`` fails — leaving TWO
rows in ``alembic_version``, which Alembic can no longer migrate from. That is silent
*corruption*, not a no-op.

The fix (``scripts/common.sh`` + ``opentr.sh``) guarantees an empty target
(``DROP DATABASE ... WITH (FORCE)`` + ``CREATE``), replays inside a single transaction so
a mid-dump failure rolls back to nothing rather than a hybrid schema, and verifies the
result before ever printing success.

This is a **static** test: it parses the shipped source and asserts the shape, the same
house style as ``test_shell_expansion_guards.py``. It does not spin up Postgres — that is
``backend/tests/integration/test_opentr_restore_roundtrip.py``'s job, against a throwaway
container. Milliseconds, ungated, runs in the fast unit suite.

**Guard the guard.** A scanner that matches nothing reports a clean tree — indistinguishable
from a shipped fix. Every detector below has a must-fire case (a synthetic snippet missing
the property) and at least one must-stay-clean case (a shape that looks similar but is not
the thing being checked, e.g. a flag mentioned only in a ``#`` comment, or a plain ``psql -c``
one-liner that never replays a dump from stdin).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPENTR = _REPO_ROOT / "opentr.sh"
_COMMON = _REPO_ROOT / "scripts" / "common.sh"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comment(line: str) -> str:
    """Drop a ``#`` comment, whole-line or trailing, without touching quoted ``#``.

    Good enough for this file's synthetic cases and the real scripts: neither uses a
    literal ``#`` inside a single/double-quoted string on any line this test reads.
    """
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return ""
    quote: str | None = None
    for i, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            return line[:i]
    return line


def extract_function(source: str, name: str) -> str:
    """Return a bash ``name() { ... }`` function's full body (braces included).

    Brace-matched rather than a fixed line count, so it survives edits inside the
    function without the test silently truncating (and passing on a half-scanned body).
    """
    match = re.search(rf"^{re.escape(name)}\s*\(\)\s*\{{", source, flags=re.MULTILINE)
    if not match:
        return ""
    start = match.end() - 1  # the opening brace itself
    depth = 0
    for i in range(start, len(source)):
        char = source[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    return source[start:]


def dump_replay_lines(text: str) -> list[str]:
    """Lines that invoke ``psql`` reading a dump from stdin (a real ``< file`` redirect).

    Deliberately narrow: a ``psql -c "SELECT ..."`` one-liner (no stdin redirect) is not a
    dump replay and must not be flagged for lacking ``--single-transaction`` — that flag is
    meaningless outside a multi-statement script read from stdin.
    """
    found = []
    for line in text.splitlines():
        code = _strip_comment(line)
        if "psql" in code and re.search(r"<\s*[\"$]", code):
            found.append(code)
    return found


def first_line_index(text: str, needle: str) -> int:
    """Index of the first line whose (comment-stripped) text contains ``needle``, or -1."""
    for i, line in enumerate(text.splitlines()):
        if needle in _strip_comment(line):
            return i
    return -1


# ---------------------------------------------------------------------------------------------
# 1. Every psql invocation that replays a dump from stdin carries both safety flags.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_dump_replay_has_on_error_stop_and_single_transaction() -> None:
    combined = _read(_OPENTR) + "\n" + _read(_COMMON)
    replay_lines = dump_replay_lines(combined)
    assert replay_lines, "expected at least one psql-from-stdin dump replay (pg_replay_dump)"
    offenders = [
        line
        for line in replay_lines
        if "-v ON_ERROR_STOP=1" not in line or "--single-transaction" not in line
    ]
    assert not offenders, (
        "dump replay(s) missing ON_ERROR_STOP=1 and/or --single-transaction "
        f"(issue #599 regression — a mid-dump failure would not roll back cleanly): {offenders}"
    )


# ---------------------------------------------------------------------------------------------
# 2. pg_drop_and_recreate_database's shape.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_pg_drop_and_recreate_database_drops_with_force_and_recreates() -> None:
    body = extract_function(_read(_COMMON), "pg_drop_and_recreate_database")
    assert body, "pg_drop_and_recreate_database not found in scripts/common.sh"
    assert "DROP DATABASE IF EXISTS" in body
    assert "WITH (FORCE)" in body
    assert "-d postgres" in body, (
        "must connect to -d postgres — cannot drop the DB you're connected to"
    )
    assert "CREATE DATABASE" in body, "drop with no matching recreate leaves no database at all"


# ---------------------------------------------------------------------------------------------
# 3. Ordering: the safety dump happens BEFORE the drop.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_safety_dump_precedes_the_drop_database_call() -> None:
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    dump_idx = first_line_index(body, "pg_dump -U")
    drop_call_idx = first_line_index(body, "pg_drop_and_recreate_database")
    assert dump_idx != -1, "expected a pg_dump safety-dump line in restore_database"
    assert drop_call_idx != -1, (
        "expected a call to pg_drop_and_recreate_database in restore_database"
    )
    assert dump_idx < drop_call_idx, (
        "the safety dump must run BEFORE the destructive drop — found the drop call at line "
        f"{drop_call_idx} but the safety dump at line {dump_idx} of the function body"
    )


# ---------------------------------------------------------------------------------------------
# 4. Confirmation: --yes flag + an actual `read -r`.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_restore_database_supports_yes_flag_and_reads_a_confirmation() -> None:
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    assert "--yes" in body
    assert re.search(r"\bread\s+-r\b", body), "expected an interactive `read -r` confirmation"


# ---------------------------------------------------------------------------------------------
# 5. Ordering: verification happens BEFORE the success message.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_success_message_follows_verification() -> None:
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    verify_idx = first_line_index(body, "pg_verify_restore")
    success_idx = first_line_index(body, "Database restored successfully")
    assert verify_idx != -1, "expected a call to pg_verify_restore in restore_database"
    assert success_idx != -1, "expected a '...Database restored successfully' message"
    assert verify_idx < success_idx, (
        "the success message must be printed AFTER verification passes — found verification "
        f"at line {verify_idx} but the success message at line {success_idx}"
    )


# ---------------------------------------------------------------------------------------------
# 6. WITH (FORCE) precondition: postgres image is >= 13 (falsifiable, not asserted).
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_postgres_image_is_new_enough_for_drop_database_with_force() -> None:
    """`DROP DATABASE ... WITH (FORCE)` is PG13+. Make the precondition checkable, not assumed."""
    compose = _read(_COMPOSE)
    match = re.search(r"image:\s*postgres:(\d+)", compose)
    assert match, "could not find a `image: postgres:<version>` line in docker-compose.yml"
    major = int(match.group(1))
    assert major >= 13, (
        f"docker-compose.yml pins postgres:{major}.x, but pg_drop_and_recreate_database uses "
        "DROP DATABASE ... WITH (FORCE), which requires PostgreSQL 13+"
    )


# ---------------------------------------------------------------------------------------------
# Guard the guard.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_dump_replay_scanner_fires_on_a_missing_flag() -> None:
    missing_single_txn = 'docker exec -i "$c" psql -v ON_ERROR_STOP=1 -U u d < "$f"\n'
    lines = dump_replay_lines(missing_single_txn)
    assert lines, "scanner failed to find the dump-replay line at all"
    assert "--single-transaction" not in lines[0]


@pytest.mark.unit
def test_dump_replay_scanner_ignores_a_commented_out_flag() -> None:
    """ON_ERROR_STOP mentioned only in a `#` comment must not count as present."""
    source = (
        "# psql -v ON_ERROR_STOP=1 --single-transaction -U u d < f  (old shape, do not use)\n"
        'docker exec -i "$c" psql --single-transaction -U u d < "$f"\n'
    )
    lines = dump_replay_lines(source)
    assert len(lines) == 1, f"expected exactly one (non-comment) dump-replay line, got {lines}"
    assert "ON_ERROR_STOP" not in lines[0]


@pytest.mark.unit
def test_dump_replay_scanner_ignores_a_plain_query_one_liner() -> None:
    """A `psql -c "SELECT ..."` one-liner has no stdin redirect — it is not a dump replay."""
    source = 'docker exec -i "$c" psql -tA -U u d -c "SELECT count(*) FROM t;"\n'
    assert dump_replay_lines(source) == []


@pytest.mark.unit
def test_extract_function_is_brace_balanced() -> None:
    """A nested `{ ... }` (an `if` block) inside the function must not truncate extraction."""
    source = "foo() {\n  if true; then\n    echo hi\n  fi\n}\n\nbar() {\n  echo unrelated\n}\n"
    body = extract_function(source, "foo")
    assert "echo hi" in body
    assert "unrelated" not in body


@pytest.mark.unit
def test_ordering_check_fires_when_reversed() -> None:
    """The ordering assertions must actually distinguish order, not just presence."""
    body = "success message here\npg_verify_restore call here\n"
    verify_idx = first_line_index(body, "pg_verify_restore")
    success_idx = first_line_index(body, "success message")
    assert not (verify_idx < success_idx), "fixture is wrong: this must be the REVERSED order"
