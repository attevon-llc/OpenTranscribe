"""Behavioural truth table for the ``./opentr.sh restore`` restart decision (issue #610).

``restore_database()`` used to unconditionally restart the app services it stopped on
every successful restore (``opentr.sh:3008`` before this fix). In a rollback scenario the
image that comes back up is the NEWER one still running from before the restore, and
because the backend runs ``alembic upgrade head`` on every startup
(``app/main.py`` lifespan -> ``app/db/migrations.py:run_migrations``), that silently
re-migrates the just-restored OLDER backup forward — destroying the very state the
restore was meant to recover. The officially documented rollback recipe
(``docs-site/docs/operations/upgrading.md``) reproduced this bug verbatim.

The fix does not refuse the restore (that would break the common "restore last night's
backup, stay on the current version" DR case) — it refuses the unattended RESTART when
the backup's alembic head does not match the live database's head read just before the
restore. That decision is extracted into a pure function,
``pg_restore_restart_decision`` (``scripts/common.sh``), so it is testable directly in a
bash subprocess with no Docker, no Postgres, and no application code involved.

This is the file that makes the truth table real evidence rather than a design-doc
table: every row below drives the SHIPPED function, not a reimplementation of its logic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMON_SH = _REPO_ROOT / "scripts" / "common.sh"


def _decide(
    dump_head: str, current_head: str, migrate_forward: str, no_restart: str
) -> tuple[str, int]:
    """Run the real, shipped ``pg_restore_restart_decision`` in a bash subprocess.

    Returns ``(stdout_stripped, returncode)``. Sourcing the real file (rather than
    re-typing the function) is the point: this proves the SHIPPED source behaves this
    way, not a copy of it that could silently drift.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "bash",
            "-c",
            f'set -uo pipefail; . "{_COMMON_SH}"; pg_restore_restart_decision "$1" "$2" "$3" "$4"',
            "_",
            dump_head,
            current_head,
            migrate_forward,
            no_restart,
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip(), result.returncode


# ---------------------------------------------------------------------------------------------
# The full truth table (plan section 5.2). Rows 1 and 4 are the "must not break normal
# use" controls — row 1 in particular guards against over-correcting into "never restart".
# ---------------------------------------------------------------------------------------------

_TRUTH_TABLE = (
    # (label, dump_head, current_head, migrate_forward, no_restart, expected_stdout, expected_rc)
    (
        "same head, no flags -> restart (the normal same-version restore must still restart)",
        "v393_x",
        "v393_x",
        "false",
        "false",
        "restart",
        0,
    ),
    (
        "older dump onto newer live head -> hold (issue #610 itself)",
        "v370_x",
        "v393_x",
        "false",
        "false",
        "hold:schema-mismatch",
        0,
    ),
    (
        "newer dump onto older live head -> hold",
        "v393_x",
        "v370_x",
        "false",
        "false",
        "hold:schema-mismatch",
        0,
    ),
    (
        "mismatch + --migrate-forward -> restart (forward migration explicitly wanted)",
        "v370_x",
        "v393_x",
        "true",
        "false",
        "restart",
        0,
    ),
    (
        "same head + --no-restart -> hold anyway (operator's explicit choice wins)",
        "v393_x",
        "v393_x",
        "false",
        "true",
        "hold:no-restart",
        0,
    ),
    (
        "dump has no alembic_version block -> hold (fail closed)",
        "",
        "v393_x",
        "false",
        "false",
        "hold:schema-mismatch",
        0,
    ),
    (
        "live DB empty/unreachable -> hold (fail closed)",
        "v393_x",
        "",
        "false",
        "false",
        "hold:schema-mismatch",
        0,
    ),
    (
        'live head is the literal "unknown" placeholder -> hold',
        "v393_x",
        "unknown",
        "false",
        "false",
        "hold:schema-mismatch",
        0,
    ),
    (
        "two alembic_version rows concatenated (issue #599 corruption shape) -> hold",
        "v393_x",
        "v370_xv393_x",
        "false",
        "false",
        "hold:schema-mismatch",
        0,
    ),
    (
        "--migrate-forward and --no-restart together -> non-zero exit, mutually exclusive",
        "v370_x",
        "v393_x",
        "true",
        "true",
        "",
        1,
    ),
)


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "label",
        "dump_head",
        "current_head",
        "migrate_forward",
        "no_restart",
        "expected",
        "expected_rc",
    ),
    _TRUTH_TABLE,
    ids=[row[0] for row in _TRUTH_TABLE],
)
def test_restart_decision_truth_table(
    label: str,
    dump_head: str,
    current_head: str,
    migrate_forward: str,
    no_restart: str,
    expected: str,
    expected_rc: int,
) -> None:
    stdout, rc = _decide(dump_head, current_head, migrate_forward, no_restart)
    assert stdout == expected, f"{label}: expected stdout {expected!r}, got {stdout!r}"
    assert rc == expected_rc, f"{label}: expected exit {expected_rc}, got {rc}"


# ---------------------------------------------------------------------------------------------
# Guard the guard: the harness itself must actually distinguish pass from fail.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_harness_can_actually_fail() -> None:
    """Feed an argument combination the function is known to reject, and prove it does.

    Without this, a broken subprocess invocation (e.g. arguments silently dropped) could
    report a fixed "restart"/rc 0 for every row and every table row would still pass —
    a scanner that matches nothing is indistinguishable from a correct one.
    """
    stdout, rc = _decide("v370_x", "v393_x", "true", "true")
    assert rc != 0, (
        "the mutually-exclusive-flags row must fail — if it didn't, the harness is broken"
    )
    assert stdout == "", f"expected no stdout on the mutually-exclusive-flags row, got {stdout!r}"


@pytest.mark.unit
def test_function_is_sourceable_standalone() -> None:
    """Sanity: scripts/common.sh sources cleanly under set -uo pipefail with no arguments.

    Guards against the function (or anything above it in the file) referencing an
    unguarded `$VAR` that only common.sh's caller (opentr.sh) would default — the same
    class of bug test_shell_expansion_guards.py polices statically, checked here by
    actually executing the sourcing.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", "-c", f'set -uo pipefail; . "{_COMMON_SH}"; echo sourced-ok'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"sourcing common.sh failed: {result.stderr}"
    assert "sourced-ok" in result.stdout
