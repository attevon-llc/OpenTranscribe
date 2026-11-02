"""The ``backup`` / ``restore`` arms of opentranscribe.sh: dispatch, flag forwarding, set -e survival, locking."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.unit._shell_extract import COMMON
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import extract_case_block
from tests.unit._shell_extract import requires_install_scripts
from tests.unit._shell_extract import run_shell

pytestmark = requires_install_scripts


# --------------------------------------------------------------------------- #
# Issue #613 — opentranscribe.sh backup/restore commands.
#
# A real production self-hosted install (curl-install / update-full) had NO shipped way to
# run backup or restore at all: opentr.sh is deliberately not on release-manifest.txt (its
# bare `docker compose` calls only work in a repo clone — see test_release_manifest.py's
# test_opentr_sh_is_not_shipped_and_the_shipped_script_covers_it), and opentranscribe.sh had
# no backup)/restore) dispatch arms. The fix promotes backup_database/restore_database out
# of opentr.sh into scripts/common.sh (parameterized by a leading compose-files chain and a
# front-end name) and wires them into both front ends.
# --------------------------------------------------------------------------- #


def test_manager_dispatches_backup_and_restore():
    """The dispatch case must actually have the arm — not just mention the word."""
    source = MANAGER.read_text()
    assert re.search(r"^\s*backup\|restore\)", source, re.MULTILINE), (
        "expected a `backup|restore)` case arm in opentranscribe.sh's dispatch"
    )


def test_manager_dispatch_control_a_commented_out_arm_does_not_count():
    """Must-fire control for the detector above."""
    synthetic = "    # backup|restore)\n    #     ...\n    #     ;;\n"
    assert not re.search(r"^\s*backup\|restore\)", synthetic, re.MULTILINE)


def test_manager_help_lists_backup_and_restore():
    """A command nobody can discover is not shipped.

    show_help is declared `function show_help { ... }` (the only function in this file using
    that style, everything else is `name() { ... }`), so it needs its own sed anchor rather
    than `extract_function`'s `name()` pattern.
    """
    fn = subprocess.run(
        ["sed", "-n", "/^function show_help {/,/^}/p", str(MANAGER)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fn.strip(), "show_help() not found in opentranscribe.sh"
    out = run_shell(fn + '\nBLUE=""; NC=""\nshow_help\n')
    assert "backup" in out and "restore" in out, f"show_help does not mention backup/restore: {out}"


def test_manager_sources_common_sh_before_its_own_fix_model_cache_permissions():
    """Position matters: bash keeps the LAST definition, so sourcing common.sh must happen
    BEFORE opentranscribe.sh's own fix_model_cache_permissions is defined, or the shared
    (and slightly different) common.sh version would silently replace the local one.
    """
    lines = MANAGER.read_text().splitlines()
    source_idx = next(
        i for i, line in enumerate(lines) if line.strip() == "if [ -f ./scripts/common.sh ]; then"
    )
    fn_idx = next(
        i for i, line in enumerate(lines) if line.startswith("fix_model_cache_permissions() {")
    )
    assert source_idx < fn_idx, (
        f"scripts/common.sh is sourced at line {source_idx + 1}, but "
        f"fix_model_cache_permissions() is defined at line {fn_idx + 1} — sourcing must come "
        "FIRST so the local definition wins (bash: last definition wins)"
    )


def test_sourcing_order_detector_fires_when_reversed():
    """Must-fire control: a synthetic file with the order reversed."""
    reversed_source = (
        "fix_model_cache_permissions() {\n  echo local\n}\n"
        "if [ -f ./scripts/common.sh ]; then\n  . ./scripts/common.sh\nfi\n"
    ).splitlines()
    source_idx = next(
        i
        for i, line in enumerate(reversed_source)
        if line.strip() == "if [ -f ./scripts/common.sh ]; then"
    )
    fn_idx = next(
        i
        for i, line in enumerate(reversed_source)
        if line.startswith("fix_model_cache_permissions() {")
    )
    assert not (source_idx < fn_idx), "fixture is wrong: this must be the REVERSED order"


def test_local_fix_model_cache_permissions_still_wins(tmp_path: Path):
    """Behavioural companion to the ordering test above: source the REAL common.sh, then the
    REAL opentranscribe.sh, and assert the definition left standing is opentranscribe.sh's
    own — identifiable by `current_owner`, a variable name that appears only in ITS version
    (common.sh's sibling loops over every subdirectory and calls the equivalent variable
    `owner`, not `current_owner`).
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "common.sh").write_text(COMMON.read_text())
    # Sourcing the whole manager also runs its trailing `case "${1:-help}" in ... esac` with
    # no args, which just prints help text — harmless, and proves nothing in the dispatch
    # itself interferes with the sourcing order under test.
    out = run_shell(f"source {MANAGER}\ndeclare -f fix_model_cache_permissions\n", cwd=tmp_path)
    assert "current_owner" in out, (
        f"opentranscribe.sh's OWN fix_model_cache_permissions did not win after sourcing "
        f"common.sh (bash: last definition wins) — got:\n{out}"
    )


def test_backup_and_restore_fail_with_a_remedy_without_common_sh(tmp_path: Path):
    """A checkout whose scripts/common.sh predates issue #613 (or is missing) must fail
    closed with a remedy, not crash on "command not found: backup_database".
    """
    (tmp_path / ".env").write_text("")
    (tmp_path / "docker-compose.yml").write_text("")
    assert not (tmp_path / "scripts").exists()
    out = run_shell(f"cd {tmp_path} && bash {MANAGER} backup 2>&1")
    assert "scripts/common.sh is missing or too old" in out, out
    assert "update-full" in out, "expected the remedy to name update-full"


def test_backup_and_restore_succeed_with_common_sh_present_no_remedy_message(tmp_path: Path):
    """Must-stay-clean companion: with common.sh present, the remedy must NOT appear."""
    (tmp_path / ".env").write_text("")
    (tmp_path / "docker-compose.yml").write_text("")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "common.sh").write_text(COMMON.read_text())
    out = run_shell(f"cd {tmp_path} && bash {MANAGER} backup --bogus-flag 2>&1")
    assert "scripts/common.sh is missing or too old" not in out, out


def test_manager_has_no_dangling_opentr_sh_instructions():
    """The shipped script must never tell a production operator to run a file they don't
    have. `opentranscribe.sh:572`'s #610 rollback preflight used to say exactly that.
    """
    offenders = [
        (i + 1, line)
        for i, line in enumerate(MANAGER.read_text().splitlines())
        if "opentr.sh" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"opentranscribe.sh names a file it does not ship: {offenders} (issue #613)"
    )


def test_dangling_reference_detector_ignores_a_commented_mention():
    """Must-stay-clean: a `#`-comment explaining the split (as this file now has) is fine."""
    source = "    # opentr.sh gets these from its prologue; this script does not.\n"
    offenders = [
        line
        for line in source.splitlines()
        if "opentr.sh" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == []


_FAKE_DOCKER = r"""
docker() {
  # `docker` is a bash FUNCTION here (functions shadow PATH binaries for unquoted
  # command lookups), so every branch below must `return`, never `exit` — `exit`
  # inside a function terminates the WHOLE calling script, not just this call, which
  # would silently truncate restore_database mid-flight and look exactly like the
  # bug this file's set-e tests exist to catch. (Caught by running this fixture
  # itself under `bash -x` and watching it die after the first failing `start` call.)
  if [ "$1" = "info" ]; then return 0; fi
  if [ "$1" != "compose" ]; then return 0; fi
  shift
  while [ "$1" = "-f" ]; do shift 2; done
  verb="$1"; shift
  case "$verb" in
    # `stop` succeeds here: restore_database's own call to it is now CHECKED (a
    # dedicated follow-up finding fixed the fact that it used to be a bare, unchecked
    # statement — see test_restore_stop_failure_aborts_before_touching_the_database for
    # the fixture that models a failing stop and proves the abort). Modelling the common
    # case here keeps every other test in this family — which only cares about reaching
    # a completed restore — from having to know about that unrelated failure mode.
    stop) return 0 ;;
    # `start` (the restart_services array, invoked only after a successful replay)
    # deliberately FAILS instead: it is still a bare, unchecked statement (written for
    # opentr.sh's non-`set -e` context — a failed restart is not itself a reason to
    # treat the restore as failed), so THIS is what test_restore_arm_survives_set_e's
    # positive/negative pair now exercises: with the adapter, the failure is silently
    # absorbed and "Database restored successfully" still prints; without it, bare
    # `set -e` aborts the script right here, before that line.
    start) return 1 ;;
    ps) return 0 ;;
    exec)
      [ "$1" = "-T" ] && shift
      svc="$1"; shift
      if [ "$svc" != "postgres" ]; then return 0; fi
      cmd="$1"
      case "$cmd" in
        pg_dump) echo "-- fake dump output"; return 0 ;;
        psql)
          args="$*"
          case "$args" in
            *"count(*) FROM alembic_version"*) echo "1" ;;
            *"SELECT version_num FROM alembic_version"*) echo "abc123" ;;
            *"information_schema.tables"*) echo "1" ;;
            *"--single-transaction"*) cat >/dev/null ;;
            *) : ;;
          esac
          return 0
          ;;
        *) return 0 ;;
      esac
      ;;
    *) return 0 ;;
  esac
}
"""


_FAKE_DUMP = (
    "-- pg_dump fake\n"
    "CREATE TABLE public.alembic_version (version_num character varying);\n"
    "COPY public.alembic_version (version_num) FROM stdin;\n"
    "abc123\n"
    "\\.\n"
)


def _backup_restore_arm() -> str:
    # No backslash before `|` or `)`: GNU sed's BRE treats a bare `|` as literal (its
    # alternation is the `\|` EXTENSION) and a bare `)` as literal too (only `\(...\)` is
    # BRE grouping) — `extract_case_block`'s other callers rely on the same bare-`)` rule
    # (e.g. `r"    update)"`). Escaping either here makes sed reject the pattern outright
    # ("Unmatched ) or \)"), which is a hard error, not a silent miss.
    return extract_case_block(MANAGER, r"    backup|restore)", r"    config)")


def test_backup_arm_passes_the_resolved_compose_chain(tmp_path: Path):
    (tmp_path / ".env").write_text("")
    body = _backup_restore_arm()
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml -f docker-compose.prod.yml"; }}
backup_database() {{ echo "BACKUP_CALL:$*"; }}
restore_database() {{ echo "RESTORE_CALL:$*"; }}
set -- backup
{body}
"""
    out = run_shell(snippet)
    assert (
        "BACKUP_CALL:-f docker-compose.yml -f docker-compose.prod.yml ./opentranscribe.sh" in out
    ), out


def test_backup_arm_control_omitting_the_chain_does_not_match():
    """Must-fire control: an arm that forgets to pass the chain produces a different call."""
    called = 'backup_database "./opentranscribe.sh"'
    assert "BACKUP_CALL:-f docker-compose.yml" not in called


def test_restore_arm_forwards_every_flag_in_order(tmp_path: Path):
    (tmp_path / ".env").write_text("")
    body = _backup_restore_arm()
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
backup_database() {{ echo "BACKUP_CALL:$*"; }}
restore_database() {{ echo "RESTORE_CALL:$*"; }}
set -- restore --yes --no-safety-dump --migrate-forward /tmp/x.sql
{body}
"""
    out = run_shell(snippet)
    assert (
        "RESTORE_CALL:-f docker-compose.yml ./opentranscribe.sh "
        "--yes --no-safety-dump --migrate-forward /tmp/x.sql" in out
    ), out


def test_restore_arm_control_a_dropped_shift_leaks_the_command_word(tmp_path: Path):
    """Must-fire control: an arm missing `shift` would forward "restore" itself as the
    first flag — prove the real arm does NOT do that.
    """
    body = _backup_restore_arm()
    assert 'cmd="$1"; shift' in body or 'cmd="$1"\n' not in body, (
        "expected the arm to shift off the command word before forwarding $@"
    )


def test_restore_arm_survives_set_e(tmp_path: Path):
    """The single most important control in this plan (issue #613).

    scripts/common.sh's restore_database is written for opentr.sh's execution semantics,
    which deliberately omit `set -e` — it has bare, unchecked `docker compose ...`
    statements (e.g. the post-restore `"${{restart_services[@]}}"` call below, whose exit
    status nothing reads) on the assumption that a failure there is non-fatal and the
    function should carry on. opentranscribe.sh runs `set -e` at the top of the file, so
    WITHOUT the `set +e` / `set -e` adapter around this call, any one of those unchecked
    commands failing would abort the WHOLE script mid-restore. (NOTE: a bare
    `[ -n "$x" ] && rm -f "$x"` — the shape ~10 of these statements take — turns out NOT to
    trip `set -e` even when $x is empty and the `&&` short-circuits: bash's errexit
    explicitly exempts a failing left-hand side of a `&&`/`||` list from triggering an
    abort, verified empirically (`bash -c 'set -e; [ -n "" ] && echo hi; echo REACHED'`
    prints REACHED). The REAL risk this test proves is the unchecked docker-compose calls,
    not that specific idiom.) This drives the REAL restore_database (sourced from the real
    common.sh), with only `docker` stubbed — and its `start` verb (used only by the
    post-restore restart, invoked once the matching-head success path is reached)
    deliberately failing — through a full --yes restore of a real plain-SQL dump.

    `stop` is deliberately NOT the failure this test injects: a follow-up finding made
    that call checked (see test_restore_stop_failure_aborts_before_touching_the_database),
    so a failing `stop` is now fatal with or without this adapter and can no longer
    demonstrate what this test exists to prove.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    assert "set +e" in body and "set -e" in body, "expected the set +e/set -e adapter in the arm"

    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    out = run_shell(snippet)
    assert "Database restored successfully" in out, out
    assert "Restarting services" in out, (
        f"expected the matching-head success path to restart services: {out}"
    )


def test_restore_arm_survives_set_e_control_aborts_without_the_adapter(tmp_path: Path):
    """Must-fire control for the test above: the SAME snippet, SAME real restore_database,
    same failing `docker compose ... start` (restart_services) call, with only the
    `set +e`/`set -e` adapter lines stripped out of the arm — under the
    opentranscribe.sh-realistic ambient `set -e`, this must die (on the unchecked `start`
    call) before completing.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    stripped_body = "\n".join(
        line for line in body.splitlines() if line.strip() not in ("set +e", "set -e")
    )

    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{stripped_body}
"""
    out = run_shell(snippet)
    assert "Database restored successfully" not in out, (
        f"expected set -e to abort mid-restore without the adapter, but it completed anyway: {out}"
    )


def test_restore_arm_reads_a_custom_postgres_db_from_env(tmp_path: Path):
    """Catches issue #613 §3.1(5): opentranscribe.sh has no `set -a; source .env` prologue
    (unlike opentr.sh), so without an explicit read of POSTGRES_DB a restore would silently
    target the DEFAULT database name on any install that customised it.
    """
    (tmp_path / ".env").write_text("POSTGRES_DB=custom_db\nPOSTGRES_USER=custom_user\n")
    body = _backup_restore_arm()
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
restore_database() {{ echo "DB_SEEN:$POSTGRES_DB USER_SEEN:$POSTGRES_USER"; }}
set -- restore --yes /tmp/x.sql
{body}
"""
    out = run_shell(snippet)
    assert "DB_SEEN:custom_db" in out, (
        f"expected the arm to export POSTGRES_DB from .env before calling restore_database: {out}"
    )
    assert "USER_SEEN:custom_user" in out, out


def test_restore_arm_control_without_the_env_read_resolves_the_wrong_database(tmp_path: Path):
    """Must-fire control: an arm that never reads .env would resolve restore_database's own
    internal `${POSTGRES_DB:-opentranscribe}` default, never `custom_db`.
    """
    (tmp_path / ".env").write_text("POSTGRES_DB=custom_db\n")
    snippet = f"""
cd {tmp_path}
restore_database() {{ echo "DB_SEEN:${{POSTGRES_DB:-opentranscribe}}"; }}
restore_database
"""
    out = run_shell(snippet)
    assert "DB_SEEN:opentranscribe" in out, (
        f"fixture is wrong: this must resolve the DEFAULT, not custom_db: {out}"
    )


def test_arms_handle_a_long_compose_chain(tmp_path: Path):
    """A 6-pair `-f` chain (base + prod + nginx + gpu + blackwell + local) must survive
    intact through every layer: the arm's call into restore_database, restore_database's
    own `local compose_files="$1"`, and every `docker compose $compose_files ...` call site
    inside it (including the restart_services array build) — all the way to a completed
    restore. A chain mangled anywhere along that path (e.g. collapsed by an errant quote, or
    truncated) breaks the fake docker stub's own `-f`-pair stripping loop, which cascades
    into `svc` resolving to something other than "postgres", every exec becoming a silent
    no-op, and restore verification failing on an empty actual_head — i.e. exactly the
    failure this test would catch, via the same "Database restored successfully" oracle
    test_restore_arm_survives_set_e uses.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    chain = (
        "-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml "
        "-f docker-compose.gpu.yml -f docker-compose.blackwell.yml -f docker-compose.local.yml"
    )
    body = _backup_restore_arm()
    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "{chain}"; }}
set -- restore --yes {dump}
{body}
"""
    out = run_shell(snippet)
    assert "Database restored successfully" in out, (
        f"a 6-pair compose chain did not survive intact through restore_database: {out}"
    )
    assert "Restarting services" in out, out


def test_long_chain_control_a_collapsed_chain_breaks_the_stub_oracle():
    """Must-fire control: proves the oracle above actually distinguishes a correctly
    word-split chain from a collapsed one, using the fake docker stub's own logic directly.
    """
    collapsed = "-f docker-compose.yml -f docker-compose.prod.yml"  # one shell word if quoted
    # Simulate what the stub does: strip "-f X" pairs one at a time. A correctly split
    # chain (the real scenario — this string arrives ALREADY split by bash, since it is
    # embedded unquoted in `docker compose $compose_files ...`) consumes cleanly:
    words = collapsed.split()
    assert words[0] == "-f" and words[2] == "-f", "fixture is not actually multi-word"
    # A collapsed chain (hypothetically passed as ONE quoted argv element) would instead
    # present as a single word starting with "-f docker-compose.yml -f ...", which does not
    # equal the literal string "-f" — the stub's `[ "$1" = "-f" ]` test fails immediately.
    single_word = [collapsed]
    assert single_word[0] != "-f", "a collapsed chain must NOT look like a bare -f flag"


def test_restore_hold_message_names_the_invoking_front_end(tmp_path: Path):
    """The schema-mismatch hold branch must tell the operator to run the front end they
    actually have. Hardcoding "./opentr.sh" here (as the pre-#613 code did at the sibling
    `--no-restart` hold-branch line) would repeat the exact defect this issue fixes.

    Drives a real schema MISMATCH: the fake docker returns a different alembic head for the
    pre-restore read (current_head) than for the post-replay read (actual_head, which must
    match the dump so verification still passes and the flow reaches the restart decision).
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    counter = tmp_path / ".head_calls"
    fake_docker = _FAKE_DOCKER.replace(
        '*"SELECT version_num FROM alembic_version"*) echo "abc123" ;;',
        f"""*"SELECT version_num FROM alembic_version"*)
              if [ ! -f {counter} ]; then
                echo x > {counter}
                echo "differenthead"
              else
                echo "abc123"
              fi
              ;;""",
    )
    body = _backup_restore_arm()
    snippet = f"""
set -e
{fake_docker}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    out = run_shell(snippet)
    assert "STOPPED on purpose" in out, f"expected to reach the hold branch: {out}"
    assert "./opentranscribe.sh start dev" in out, out
    assert "./opentr.sh start dev" not in out, (
        f"the hold message hardcoded the wrong front end: {out}"
    )


# --------------------------------------------------------------------------- #
# Follow-up findings from a second adversarial audit of the #613/#614/#615
# session: restore_database's own return code, its unchecked `stop`, and a
# lock against two concurrent restores.
# --------------------------------------------------------------------------- #


def test_restore_no_safety_dump_control_demonstrates_the_bug_mechanism():
    """Must-fire control: the exact bash trap the real fix addresses.

    A function whose LAST statement is `[ -n "$x" ] && echo ...` returns 1 when $x is
    empty, even though the function did everything it was asked to do — the truthiness
    of an unrelated conditional becomes the function's own return code. This proves the
    trap is real bash behaviour, not a hypothetical, before trusting the positive test
    below that proves restore_database no longer falls into it.
    """
    proc = subprocess.run(
        ["bash", "-c", 'f() { x=""; [ -n "$x" ] && echo "x is set"; }; f; echo "rc=$?"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rc=1" in proc.stdout, (
        f"fixture is wrong: expected the bare `[ -n ... ] && echo` idiom to return 1 "
        f"when unset: {proc.stdout!r}"
    )


def test_restore_no_safety_dump_still_exits_zero_on_success(tmp_path: Path):
    """The actual regression: restore_database's last statement used to be
    `[ -n "$safety_dump_file" ] && echo ...`, and with `--no-safety-dump`
    `$safety_dump_file` is empty — so that conditional's own exit status (1) silently
    became restore_database's return code, on a completely successful restore.
    opentranscribe.sh's `backup|restore)` arm does `rc=$?; set -e; exit $rc` right after
    calling in, so `./opentranscribe.sh restore --yes --no-safety-dump <file>` exited 1
    after a clean restore. This drives the REAL restore_database end to end (real
    common.sh, only `docker` stubbed) and asserts the actual PROCESS exit code, not just
    the printed output — `run_shell` elsewhere in this file discards the return code
    entirely, which is exactly how this class of bug stays invisible to a test suite.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes --no-safety-dump {dump}
{body}
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip()
    assert "Database restored successfully" in out, out
    assert proc.returncode == 0, (
        f"restore --yes --no-safety-dump exited {proc.returncode} despite a successful "
        f"restore (expected 0): {out}"
    )


def test_restore_stop_failure_aborts_before_touching_the_database(tmp_path: Path):
    """A failing `docker compose ... stop` must abort the restore, not be silently
    absorbed. Every OTHER destructive step in restore_database (the safety dump, the
    drop/recreate, the replay, the verify) is already guarded with an explicit
    check-and-abort — this proves `stop` now is too: with the backend/celery services
    still possibly connected, proceeding to DROP DATABASE would be exactly the hazard
    the other guards exist to prevent.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    fake_docker = _FAKE_DOCKER.replace("stop) return 0 ;;", "stop) return 1 ;;")
    body = _backup_restore_arm()
    snippet = f"""
set -e
{fake_docker}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    out = run_shell(snippet)
    assert "Could not stop application services" in out, out
    assert "Database restored successfully" not in out, (
        f"a failing stop must abort the restore, not complete it: {out}"
    )
    assert "Dropping and recreating" not in out, (
        f"restore_database must never reach DROP DATABASE when the stop it depends on failed: {out}"
    )


def test_restore_stop_failure_control_the_fixture_actually_fails_stop():
    """Must-fire control: the `.replace()` above targets the exact stub line — if that
    line's text ever changes, the replace silently no-ops and the test above would pass
    for the wrong reason (stop still succeeding).
    """
    assert "stop) return 0 ;;" in _FAKE_DOCKER, (
        "fixture text changed — update the .replace() in the test above to match"
    )


def test_restore_refuses_while_another_restore_holds_the_lock(tmp_path: Path):
    """Two concurrent restore_database invocations must not race on the destructive
    portion: both would read current_head, both would take a safety dump, and the
    second's DROP DATABASE ... WITH (FORCE) could kill the first's replay mid-transaction.
    Simulated deterministically (no real timing race) by pre-acquiring the exact lock
    file restore_database uses from a background holder, polling until it is actually
    held — the same `flock -n -w 0 <file> true` technique
    scripts/safe-precommit-selftest.sh uses for the identical class of hazard — before
    driving a real restore attempt against it.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    (tmp_path / "backups").mkdir()
    lock_file = tmp_path / "backups" / ".restore.lock"

    holder = subprocess.Popen(
        ["bash", "-c", f'exec 9>"{lock_file}"; flock 9; sleep 10'],
    )
    try:
        for _ in range(50):
            probe = subprocess.run(["bash", "-c", f'flock -n -w 0 "{lock_file}" true'], check=False)
            if probe.returncode != 0:
                break
            time.sleep(0.1)
        else:
            holder.terminate()
            pytest.fail("background holder never actually acquired the lock")

        body = _backup_restore_arm()
        snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
        out = run_shell(snippet)
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGTERM didn't land in time (sleep 10 ignoring it would be unusual, but a
            # leaked process holding the lock file open would make every OTHER test in
            # this module that touches a `.restore.lock` flaky) — escalate rather than
            # silently leaving it running.
            holder.kill()
            holder.wait(timeout=5)

    assert "already in progress" in out, out
    assert "Database restored successfully" not in out, (
        f"a restore must not proceed while another one holds the lock: {out}"
    )
    assert "Dropping and recreating" not in out, (
        f"restore_database must refuse before ever reaching DROP DATABASE: {out}"
    )


def test_restore_lock_is_released_after_a_successful_restore(tmp_path: Path):
    """Regression guard: the lock must not leak. Two SEQUENTIAL restores against the
    same directory (same lock file) must both succeed — if the first restore left the
    lock held, the second would report "already in progress" for no real reason.

    Two SEPARATE subprocess invocations, not two calls inside one snippet: the arm's
    own body ends in `exit $rc`, so a second `{{body}}` chained after the first inside
    one bash process would never even run — exactly matching how two real, sequential
    `./opentranscribe.sh restore ...` invocations actually happen (two processes).
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    first = run_shell(snippet)
    second = run_shell(snippet)
    assert "Database restored successfully" in first, first
    assert "already in progress" not in second, (
        f"the lock from the first restore leaked into the second: {second}"
    )
    assert "Database restored successfully" in second, second
