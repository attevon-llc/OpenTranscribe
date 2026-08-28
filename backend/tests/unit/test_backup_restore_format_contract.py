"""Static shape checks for the scheduled `-Fc` backup's restore path (issue #600).

``backend/app/services/backup_service.run_pg_dump`` produces ``pg_dump --format=custom``
(``-Fc``) output. Before this fix, ``pg_restore`` — the only tool that reads that format —
appeared nowhere in this repo: ``./opentr.sh restore`` sniffed the ``PGDMP`` magic bytes and
printed a hint command that was itself corrupting (fixing its missing stdin redirect
reproduces #599's exact silent-corruption bug — drifted data survives, ``alembic_version``
ends with two rows). The fix turns that dead end into a second replay branch inside the same
``restore_database`` function, reusing #599's confirm / safety-dump / drop-recreate / verify
machinery, with ``pg_restore`` swapped in for the replay step.

These are the **cheap** tests that make the expensive ones (``backend/tests/integration/
test_scheduled_backup_restore_roundtrip.py``) un-skippable-in-silence: a fast static pin on
``--format=custom`` so a future format change fails loudly in THIS suite, plus static parses
of ``opentr.sh``/``scripts/common.sh`` mirroring ``test_opentr_restore_safety.py``'s house
style — brace-matched function extraction, comment-stripped line scanning, and a must-fire +
must-stay-clean case for every detector so a scanner that matches nothing cannot pass as a
clean tree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from app.services import backup_service as bs
from tests.unit.test_opentr_restore_safety import extract_function
from tests.unit.test_opentr_restore_safety import first_line_index

_REPO_ROOT = Path(__file__).resolve().parents[3]
# The moved-to-common.sh code this file exercises (issue #613): restore_database now lives
# in scripts/common.sh, not opentr.sh — see test_opentr_restore_safety.py's _COMMON for the
# same rationale.
_COMMON = _REPO_ROOT / "scripts" / "common.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comment(line: str) -> str:
    """Same comment-stripping rule as test_opentr_restore_safety.py (kept in sync deliberately:
    both files scan the same two shell scripts and must treat a `#`-only mention identically).
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


# ---------------------------------------------------------------------------------------------
# 1. run_pg_dump still asks pg_dump for custom format — both the constant AND the real argv.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_pg_dump_format_constant_is_custom() -> None:
    assert bs.PG_DUMP_FORMAT == "custom", (
        "scripts/common.sh's pg_replay_custom_dump/pg_verify_custom_restore and opentr.sh's "
        "PGDMP magic-byte dispatch only understand pg_dump's custom (-Fc) format. Changing "
        "PG_DUMP_FORMAT without changing those too breaks every existing .dump backup's "
        "restore path (issue #600)."
    )


def _captured_pg_dump_argv(tmp_path: Path) -> list[str]:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        out = kwargs.get("stdout")
        if out is not None:
            out.write(b"FAKE")
            out.flush()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with mock.patch("app.services.backup_service.subprocess.run", side_effect=fake_run):
        bs.run_pg_dump(
            tmp_path / "probe.dump",
            encrypt=False,
            passphrase_file="",
            database_url="postgresql://u:p@h/db",
        )
    return captured["cmd"]


@pytest.mark.unit
def test_run_pg_dump_still_produces_custom_format(tmp_path: Path) -> None:
    argv = _captured_pg_dump_argv(tmp_path)
    assert "--format=custom" in argv, (
        f"run_pg_dump's actual pg_dump invocation dropped --format=custom: {argv}. This is "
        "the exact regression #600 exists to catch — the restore path (pg_restore-based) "
        "would silently stop matching every backup produced from here on."
    )


@pytest.mark.unit
def test_argv_format_check_fires_on_a_synthetic_plain_sql_switch() -> None:
    """Must-fire control for the assertion above: a synthetic argv without the flag fails."""
    synthetic_argv = ["pg_dump", "--no-owner", "--no-acl", "--dbname", "postgresql://x"]
    assert "--format=custom" not in synthetic_argv


# ---------------------------------------------------------------------------------------------
# 2. opentr.sh dispatches into pg_restore instead of erroring out on PGDMP.
# ---------------------------------------------------------------------------------------------


def _pgdmp_branch(body: str) -> str:
    """Extract the `if ... PGDMP ...` branch's body out of restore_database, brace-matched."""
    match = re.search(r'if \[ "\$\(head -c 5[^\n]*PGDMP"[^\n]*\]; then', body)
    if not match:
        return ""
    # Find the matching `fi` for this `if`, tracking nested if/fi pairs.
    depth = 1
    pos = match.end()
    for kw_match in re.finditer(r"\b(if|fi)\b", body[pos:]):
        if kw_match.group(1) == "if":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return body[match.start() : pos + kw_match.end()]
    return body[match.start() :]


@pytest.mark.unit
def test_opentr_restore_dispatches_on_pgdmp_instead_of_erroring() -> None:
    body = extract_function(_read(_COMMON), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"
    branch = _pgdmp_branch(body)
    assert branch, "expected a PGDMP magic-byte branch inside restore_database"
    assert not re.search(r"\bexit 1\b", branch), (
        "the PGDMP branch must dispatch into the pg_restore replay path, not error out "
        "(issue #600 — the old dead-end printed a corrupting hint command and exited 1)"
    )
    assert "pg_replay_custom_dump" in body, (
        "restore_database must reach pg_replay_custom_dump on the custom-format path"
    )


@pytest.mark.unit
def test_pgdmp_branch_scanner_fires_on_the_old_dead_end() -> None:
    """Must-fire control: a synthetic snippet that still errors out on PGDMP."""
    synthetic = (
        "restore_database() {\n"
        '  if [ "$(head -c 5 "$restore_source" 2>/dev/null)" = "PGDMP" ]; then\n'
        '    echo "not supported"\n'
        "    exit 1\n"
        "  fi\n"
        "}\n"
    )
    branch = _pgdmp_branch(extract_function(synthetic, "restore_database"))
    assert branch, "scanner failed to find the synthetic PGDMP branch at all"
    assert re.search(r"\bexit 1\b", branch)


# ---------------------------------------------------------------------------------------------
# 3. pg_replay_custom_dump carries --exit-on-error and --single-transaction, never -j/--jobs.
# ---------------------------------------------------------------------------------------------


def _custom_replay_lines(text: str) -> list[str]:
    """Non-comment lines invoking pg_restore reading a dump from stdin (a real `< file`)."""
    found = []
    for line in text.splitlines():
        code = _strip_comment(line)
        if "pg_restore" in code and re.search(r"<\s*[\"$]", code):
            found.append(code)
    return found


@pytest.mark.unit
def test_custom_replay_has_exit_on_error_and_single_transaction() -> None:
    body = extract_function(_read(_COMMON), "pg_replay_custom_dump")
    assert body, "pg_replay_custom_dump not found in scripts/common.sh"
    lines = _custom_replay_lines(body)
    assert lines, "expected at least one pg_restore-from-stdin replay line"
    offenders = [
        line
        for line in lines
        if "--exit-on-error" not in line or "--single-transaction" not in line
    ]
    assert not offenders, (
        f"pg_restore replay missing --exit-on-error and/or --single-transaction: {offenders}"
    )


@pytest.mark.unit
def test_custom_replay_does_not_use_parallel_jobs() -> None:
    body = extract_function(_read(_COMMON), "pg_replay_custom_dump")
    assert body, "pg_replay_custom_dump not found in scripts/common.sh"
    lines = _custom_replay_lines(body)
    assert lines, "expected at least one pg_restore-from-stdin replay line"
    offenders = [line for line in lines if re.search(r"(^|\s)(-j\b|--jobs\b)", line)]
    assert not offenders, (
        "pg_restore -j/--jobs is mutually exclusive with --single-transaction (measured: "
        f"'pg_restore: error: cannot specify both --single-transaction and multiple jobs'): {offenders}"
    )


@pytest.mark.unit
def test_custom_replay_scanner_fires_on_missing_single_transaction() -> None:
    """Must-fire control for the flags check."""
    missing = 'docker exec -i "$c" pg_restore -U u -d d --exit-on-error < "$f"\n'
    lines = _custom_replay_lines(missing)
    assert lines, "scanner failed to find the pg_restore-from-stdin line at all"
    assert "--single-transaction" not in lines[0]


@pytest.mark.unit
def test_custom_replay_scanner_fires_on_a_synthetic_dash_j() -> None:
    """Must-fire control for the -j/--jobs check."""
    with_jobs = (
        'docker exec -i "$c" pg_restore -U u -d d --exit-on-error --single-transaction '
        '-j 4 < "$f"\n'
    )
    lines = _custom_replay_lines(with_jobs)
    assert lines, "scanner failed to find the pg_restore-from-stdin line at all"
    assert re.search(r"(^|\s)(-j\b|--jobs\b)", lines[0])


@pytest.mark.unit
def test_custom_replay_scanner_ignores_a_list_only_one_liner() -> None:
    """A `pg_restore --list < file` one-liner never replays — must not be flagged."""
    source = 'docker exec -i "$c" pg_restore --list < "$f"\n'
    lines = _custom_replay_lines(source)
    assert len(lines) == 1  # the scanner finds the line...
    # ...but it is exempt from the flag checks by virtue of not being a *replay* — the
    # verifier calls this shape directly (pg_verify_custom_restore), never through
    # pg_replay_custom_dump, so the flags check above only ever scans the replay function's
    # own body, not this line. This case documents that distinction rather than asserting
    # on it a second time.


# ---------------------------------------------------------------------------------------------
# 4. The TOC table-count filter excludes "TABLE DATA" entries (measured: 4 vs 2 overcount).
# ---------------------------------------------------------------------------------------------

_TOC_FIXTURE = """\
1234; 0 0 ENCODING - ENCODING
1235; 0 0 SCHEMA - public postgres
1236; 1259 0 TABLE public alembic_version postgres
1237; 0 0 TABLE DATA public alembic_version postgres
1238; 1259 0 TABLE public media_file postgres
1239; 0 0 TABLE DATA public media_file postgres
"""


def _count_toc_tables_correct(toc: str) -> int:
    count = 0
    for line in toc.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[3] == "TABLE" and fields[4] != "DATA":
            count += 1
    return count


def _count_toc_tables_naive(toc: str) -> int:
    """The rejected shape — kept here only to prove the fixture actually discriminates."""
    return sum(1 for line in toc.splitlines() if " TABLE " in line)


@pytest.mark.unit
def test_toc_table_count_excludes_table_data_entries() -> None:
    assert _count_toc_tables_correct(_TOC_FIXTURE) == 2


@pytest.mark.unit
def test_naive_toc_filter_overcounts_on_the_same_fixture() -> None:
    """Must-fire control: proves the fixture actually distinguishes correct from naive."""
    assert _count_toc_tables_naive(_TOC_FIXTURE) == 4


@pytest.mark.unit
def test_pg_verify_custom_restore_uses_the_correct_toc_filter_not_grep() -> None:
    body = extract_function(_read(_COMMON), "pg_verify_custom_restore")
    assert body, "pg_verify_custom_restore not found in scripts/common.sh"
    assert "pg_restore --list" in body or "pg_restore  --list" in body.replace("\\\n", " "), (
        "pg_verify_custom_restore should derive expected_tables from `pg_restore --list` (the "
        "archive's own TOC), not the plain-SQL verifier's grep-over-CREATE-TABLE"
    )
    offenders = [
        line
        for line in body.splitlines()
        if "grep" in _strip_comment(line) and "TABLE" in line and "$4" not in line
    ]
    assert not offenders, (
        f"found a grep-based TABLE count instead of the awk $4/$5 TOC filter: {offenders}"
    )
    assert '$4 == "TABLE"' in body and '$5 != "DATA"' in body, (
        'expected the awk filter \'$4 == "TABLE" && $5 != "DATA"\' — the naive '
        "`grep -c ' TABLE '` overcounts by matching TABLE DATA entries too (measured: 4 vs 2)"
    )


# ---------------------------------------------------------------------------------------------
# 5. Ordering: the safety dump precedes the drop on the custom (pg_restore) path too.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_safety_dump_precedes_the_drop_on_the_custom_path_too() -> None:
    body = extract_function(_read(_COMMON), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"
    dump_idx = first_line_index(body, "pg_dump -U")
    drop_call_idx = first_line_index(body, "pg_drop_and_recreate_database")
    assert dump_idx != -1, "expected a pg_dump safety-dump line in restore_database"
    assert drop_call_idx != -1, (
        "expected a call to pg_drop_and_recreate_database in restore_database"
    )
    assert dump_idx < drop_call_idx, (
        "the safety dump (plain pg_dump, shared by both formats) must run BEFORE the "
        f"destructive drop — dump at line {dump_idx}, drop at line {drop_call_idx}"
    )
    # And the drop itself must be shared — one drop/recreate call point, not a duplicate
    # custom-format-only copy, or the two paths could silently diverge in what "empty
    # target" means.
    assert body.count("pg_drop_and_recreate_database") == 1, (
        "expected exactly one pg_drop_and_recreate_database call, shared by both the plain "
        f"and custom restore paths, found {body.count('pg_drop_and_recreate_database')}"
    )


# ---------------------------------------------------------------------------------------------
# 6. Ordering: the success message follows custom verification too.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_success_message_follows_custom_verification() -> None:
    body = extract_function(_read(_COMMON), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"
    verify_idx = first_line_index(body, "pg_verify_custom_restore")
    success_idx = first_line_index(body, "Database restored successfully")
    assert verify_idx != -1, "expected a call to pg_verify_custom_restore in restore_database"
    assert success_idx != -1, "expected a '...Database restored successfully' message"
    assert verify_idx < success_idx, (
        "the success message must be printed AFTER custom-format verification passes — "
        f"verification at line {verify_idx}, success message at line {success_idx}"
    )


# ---------------------------------------------------------------------------------------------
# 7. .gpg decryption must still precede the PGDMP magic-byte sniff (load-bearing ordering).
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_gpg_decryption_precedes_the_pgdmp_sniff() -> None:
    body = extract_function(_read(_COMMON), "restore_database")
    assert body, "restore_database not found in scripts/common.sh"
    gpg_idx = first_line_index(body, "gpg --yes --output")
    sniff_idx = first_line_index(body, 'head -c 5 "$restore_source"')
    assert gpg_idx != -1, "expected the gpg decryption call in restore_database"
    assert sniff_idx != -1, "expected the PGDMP magic-byte sniff in restore_database"
    assert gpg_idx < sniff_idx, (
        "decryption must run BEFORE the magic-byte sniff, or a .dump.gpg file never reaches "
        f"the custom-format branch — decryption at line {gpg_idx}, sniff at line {sniff_idx}"
    )
