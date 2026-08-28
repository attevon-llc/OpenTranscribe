"""Static shape checks for the ``./opentr.sh restore`` fixes (issues #599, #610).

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
# Issue #610 detectors. `restore_database()` used to unconditionally restart the app
# services it had stopped on every code path — success, failed replay, failed verify —
# which is precisely how a restore of an OLDER backup got silently migrated back FORWARD
# by the still-running NEWER image. The detectors below assert the shape of the fix, not
# its behaviour (that is test_restore_restart_decision.py, against the real shipped
# scripts/common.sh function, and test_opentr_restore_roundtrip.py, against real Postgres).
# ---------------------------------------------------------------------------------------------


def head_read_precedes_gate(body: str) -> bool:
    """True if the live-head read happens before the ``--yes``-skippable confirm gate."""
    head_idx = first_line_index(body, "SELECT version_num FROM alembic_version")
    gate_idx = first_line_index(body, 'if [ "$skip_confirm" != true ]')
    return head_idx != -1 and gate_idx != -1 and head_idx < gate_idx


def final_restart_is_guarded(body: str) -> bool:
    """True if the LAST ``restart_services`` call sits in an ``else`` branch that follows
    a ``hold_reason``-style decision variable — i.e. the restart is conditional, not
    unconditional on the success path.
    """
    calls = [m.start() for m in re.finditer(r'"\$\{restart_services\[@\]\}"', body)]
    if not calls:
        return False
    window = body[: calls[-1]]
    hold_idx = window.rfind("hold_reason")
    else_idx = window.rfind("else")
    return hold_idx != -1 and else_idx != -1 and else_idx > hold_idx


def restart_calls_between(body: str, start_needle: str, end_needle: str) -> list[str]:
    """``restart_services`` call lines strictly between two (comment-stripped) markers."""
    lines = body.splitlines()
    start_idx = first_line_index(body, start_needle)
    end_idx = first_line_index(body, end_needle)
    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        return []
    segment = lines[start_idx:end_idx]
    return [line for line in segment if "restart_services[@]" in _strip_comment(line)]


def case_arms(body: str) -> set[str]:
    """Bash ``case`` arm labels of the form ``--flag)`` (comment-stripped)."""
    arms: set[str] = set()
    for line in body.splitlines():
        stripped = _strip_comment(line).strip()
        if stripped.startswith("--") and stripped.endswith(")"):
            arms.add(stripped[:-1])
    return arms


_MUTEX_RE = re.compile(
    r'\[\s*"\$migrate_forward"\s*=\s*true\s*\]\s*&&\s*\[\s*"\$no_restart"\s*=\s*true\s*\]'
)


def has_mutex_check(body: str) -> bool:
    """True if an explicit ``--migrate-forward`` + ``--no-restart`` exclusivity check exists."""
    stripped = "\n".join(_strip_comment(line) for line in body.splitlines())
    return bool(_MUTEX_RE.search(stripped))


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
# 7. The live head is read BEFORE the --yes-skippable confirmation gate (issue #610).
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_current_head_is_read_before_the_confirmation_gate() -> None:
    """Pre-fix, ``current_head`` was read only inside ``if [ "$skip_confirm" != true ]``,

    so ``--yes`` — the exact path a rollback rehearsal and a scripted DR restore both
    use — never learned the running image's schema head, and so could never detect a
    mismatch with it.
    """
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    assert head_read_precedes_gate(body), (
        "expected the live current_head read to precede the "
        '`if [ "$skip_confirm" != true ]` gate (issue #610 — --yes must not skip it)'
    )


# ---------------------------------------------------------------------------------------------
# 8. The success-path restart is conditional on the restart decision, not unconditional.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_success_restart_is_conditional() -> None:
    """Pre-fix, the success path called ``"${restart_services[@]}"`` unconditionally —

    restarting the previously-running (in a rollback scenario, NEWER) image regardless
    of whether the schema it just saw restored matches what it expects. That silent
    forward re-migration is issue #610 itself.
    """
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    assert "pg_restore_restart_decision" in body, (
        "expected restore_database to consult the shared pg_restore_restart_decision "
        "(scripts/common.sh) before deciding whether to restart"
    )
    assert final_restart_is_guarded(body), (
        "expected the final restart_services call to be inside an `else` branch guarded "
        "by a hold_reason-style decision variable, not run unconditionally"
    )


# ---------------------------------------------------------------------------------------------
# 9. A failed replay or a failed verify must NOT restart services (issue #610, worse variant).
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_failed_replay_and_verify_do_not_restart() -> None:
    """Restarting after a failed replay/verify is arguably worse than the success-path bug:

    the database is EMPTY (failed replay, rolled back) or unverified (failed verify), so
    restarting the newer backend would take run_migrations()'s "empty database" branch
    and seed a fresh admin over nothing, turning a failed restore into a silently
    brand-new empty deployment.
    """
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"

    replay_fail_idx = first_line_index(body, "the database is now empty")
    verify_fail_idx = first_line_index(body, "Restore verification failed")
    decision_idx = first_line_index(body, "pg_restore_restart_decision")
    assert replay_fail_idx != -1, "expected the replay-failure message"
    assert verify_fail_idx != -1, "expected the verify-failure message"
    assert decision_idx != -1, "expected a call to pg_restore_restart_decision"
    assert replay_fail_idx < verify_fail_idx < decision_idx, (
        "expected replay-failure, then verify-failure, then the restart decision, in that order"
    )

    replay_offenders = restart_calls_between(
        body, "the database is now empty", "Restore verification failed"
    )
    verify_offenders = restart_calls_between(
        body, "Restore verification failed", "pg_restore_restart_decision"
    )
    assert not replay_offenders, (
        f"replay-failure path must NOT restart services (issue #610): {replay_offenders}"
    )
    assert not verify_offenders, (
        f"verify-failure path must NOT restart services (issue #610): {verify_offenders}"
    )


# ---------------------------------------------------------------------------------------------
# 10. --migrate-forward and --no-restart exist as real flags, and are mutually exclusive.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_restore_supports_migrate_forward_and_no_restart_flags() -> None:
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    arms = case_arms(body)
    assert {"--migrate-forward", "--no-restart"} <= arms, (
        f"expected --migrate-forward and --no-restart as case arms in restore_database's "
        f"arg parser, found: {sorted(arms)}"
    )


@pytest.mark.unit
def test_migrate_forward_and_no_restart_are_mutually_exclusive() -> None:
    body = extract_function(_read(_OPENTR), "restore_database")
    assert body, "restore_database not found in opentr.sh"
    assert has_mutex_check(body), (
        'expected an explicit `[ "$migrate_forward" = true ] && [ "$no_restart" = true ]` '
        "(or equivalent) rejection of the combination"
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


# ---------------------------------------------------------------------------------------------
# Guard the guard, part 2 — the issue #610 detectors. Each has a must-fire (the property
# is missing) and at least one must-stay-clean (a shape that looks similar but is not the
# thing being checked) case.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_head_read_order_detector_fires_when_reversed() -> None:
    reversed_order = (
        'if [ "$skip_confirm" != true ]; then\n'
        "  :\n"
        "fi\n"
        'current_head="$(psql -c "SELECT version_num FROM alembic_version;")"\n'
    )
    assert not head_read_precedes_gate(reversed_order)


@pytest.mark.unit
def test_head_read_order_detector_stays_clean_in_correct_order() -> None:
    correct_order = (
        'current_head="$(psql -c "SELECT version_num FROM alembic_version;")"\n'
        'if [ "$skip_confirm" != true ]; then\n'
        "  :\n"
        "fi\n"
    )
    assert head_read_precedes_gate(correct_order)


@pytest.mark.unit
def test_unconditional_restart_detector_fires_on_a_bare_call() -> None:
    """The pre-fix shape: no hold_reason, no else, just a bare restart on success."""
    unconditional = 'echo "Restarting..."\n"${restart_services[@]}"\necho "done"\n'
    assert not final_restart_is_guarded(unconditional)


@pytest.mark.unit
def test_unconditional_restart_detector_ignores_a_restart_mentioned_only_in_a_comment() -> None:
    """A restart_services call named only in a `#` comment must not count as guarded OR present."""
    comment_only = '# call "${restart_services[@]}" here eventually\necho "todo"\n'
    assert not final_restart_is_guarded(comment_only)


@pytest.mark.unit
def test_conditional_restart_detector_stays_clean_on_the_real_shape() -> None:
    guarded = (
        'local hold_reason=""\n'
        'if [ -n "$hold_reason" ]; then\n'
        '  echo "held"\n'
        "else\n"
        '  "${restart_services[@]}"\n'
        "fi\n"
    )
    assert final_restart_is_guarded(guarded)


@pytest.mark.unit
def test_restart_between_detector_fires_on_a_restart_in_the_failure_path() -> None:
    body = (
        "echo the database is now empty\n"
        '"${restart_services[@]}"\n'
        "echo Restore verification failed\n"
    )
    offenders = restart_calls_between(
        body, "the database is now empty", "Restore verification failed"
    )
    assert offenders == ['"${restart_services[@]}"'], (
        f"expected exactly the one restart call between the two markers to be found, got {offenders}"
    )


@pytest.mark.unit
def test_restart_between_detector_stays_clean_with_no_restart_in_between() -> None:
    body = "echo the database is now empty\nexit 1\necho Restore verification failed\n"
    offenders = restart_calls_between(
        body, "the database is now empty", "Restore verification failed"
    )
    assert offenders == []


@pytest.mark.unit
def test_case_arms_detector_fires_when_a_flag_is_missing() -> None:
    only_one_flag = "  --migrate-forward)\n    migrate_forward=true\n    shift\n    ;;\n"
    assert case_arms(only_one_flag) == {"--migrate-forward"}


@pytest.mark.unit
def test_case_arms_detector_ignores_a_commented_out_arm() -> None:
    """A flag mentioned only in a `#` comment is not a real case arm."""
    commented = "# --no-restart)  (old shape, do not use)\n"
    assert case_arms(commented) == set()


@pytest.mark.unit
def test_mutex_check_detector_fires_when_missing() -> None:
    no_check = 'if [ "$migrate_forward" = true ]; then\n  echo hi\nfi\n'
    assert not has_mutex_check(no_check)


@pytest.mark.unit
def test_mutex_check_detector_ignores_a_commented_out_check() -> None:
    commented = '# [ "$migrate_forward" = true ] && [ "$no_restart" = true ] -> error, old shape\n'
    assert not has_mutex_check(commented)


@pytest.mark.unit
def test_mutex_check_detector_stays_clean_on_the_real_shape() -> None:
    real_shape = (
        'if [ "$migrate_forward" = true ] && [ "$no_restart" = true ]; then\n  exit 1\nfi\n'
    )
    assert has_mutex_check(real_shape)
