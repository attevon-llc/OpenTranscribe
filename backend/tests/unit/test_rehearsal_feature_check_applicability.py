"""A feature check that exercises a migration is judged only when that migration runs in the upgrade.

The 0.6.0 -> 0.6.1 rehearsal failed two checks because they seeded duplicate ACTIVE ``task`` rows
for migration v448 to clean up, but v448 had already shipped in v0.6.0: the FROM schema carried the
unique index and refused the seed ("the FROM schema refused the task rows"). The premise (v448 is
new in TO) was hard-wired. It is now derived from the FROM release's alembic graph, and a check
whose migration the FROM release already ships is recorded as N/A, which is neither a pass nor a
skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB = REPO_ROOT / "scripts" / "release-tests" / "lib"
ALEMBIC_HEAD = LIB / "alembic-head.py"


def _tree(tmp_path: Path, revisions: list[tuple[str, str | None]]) -> Path:
    """A throwaway ``backend/alembic/versions`` with the given (revision, down_revision) chain."""
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    for rev, down in revisions:
        down_src = "None" if down is None else f'"{down}"'
        (versions / f"{rev}.py").write_text(f'revision = "{rev}"\ndown_revision = {down_src}\n')
    return tmp_path / "backend"


def _has(backend: Path, rev: str) -> int:
    return subprocess.run(
        ["python3", str(ALEMBIC_HEAD), str(backend), "--has-revision", rev],
        capture_output=True,
        text=True,
    ).returncode


CHAIN = [
    ("v100_base", None),
    ("v448_task_one_active_row", "v100_base"),
    ("v449_backfill", "v448_task_one_active_row"),
]


def test_has_revision_reads_the_chain_not_a_table(tmp_path: Path) -> None:
    backend = _tree(tmp_path, CHAIN)
    assert _has(backend, "v448") == 0  # prefix before the first underscore
    assert _has(backend, "v449_backfill") == 0  # full id, and the head itself
    assert _has(backend, "v450") == 1  # newer than the FROM tree: the upgrade will run it
    assert _has(backend, "v44") == 1  # a prefix of the number is not the id


def test_a_revision_off_the_head_chain_does_not_count(tmp_path: Path) -> None:
    # A file whose id exists but is not an ancestor of the head must not read as "shipped".
    backend = _tree(tmp_path, [*CHAIN, ("v900_orphan_branch", "v100_base")])
    # Two heads: the chain is malformed, which is an error (2), never a guessed answer.
    assert _has(backend, "v448") == 2


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -uo pipefail; REPO_ROOT={REPO_ROOT}; TEST_REPORT_FILE=/dev/null\n"
            f"source {LIB}/assertions.sh; source {LIB}/versions.sh; source {LIB}/feature-checks.sh\n"
            f"{script}",
        ],
        capture_output=True,
        text=True,
    )


def test_a_check_for_a_migration_from_already_ships_is_reported_not_applicable(
    tmp_path: Path,
) -> None:
    backend = _tree(tmp_path, CHAIN)
    out = _bash(
        f"FC_FROM_BACKEND_DIR={backend}; FROM_VERSION=v0.6.0\n"
        'fc_judge "upgrade: v448 resolved the seeded rows" v448-tasks\n'
        'fc_judge "upgrade: v449 backfill recorded" backfill-record\n'
        "as_summary"
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.count("N/A") == 2
    assert "not applicable: v448 shipped in FROM v0.6.0" in out.stdout
    # Counted on its own line: not a pass, not a skip, and it does not turn the run red.
    assert "0/0 assertions passed (0 skipped, 2 not applicable)" in out.stdout


def test_a_migration_the_upgrade_runs_is_still_judged(tmp_path: Path) -> None:
    older = _tree(tmp_path, CHAIN[:1])  # FROM predates v448: the check applies
    out = _bash(
        f"FC_FROM_BACKEND_DIR={older}; FROM_VERSION=v0.5.1\n"
        'FC_BODY=""; fc_judge "upgrade: v448 resolved the seeded rows" v448-tasks\n'
        "as_summary"
    )
    assert "N/A" not in out.stdout, "a migration the upgrade runs must still be verified"
    assert "FAIL" in out.stdout  # judged for real: an empty snapshot is a failure, not a pass


def test_a_fresh_install_without_a_from_tree_is_unaffected(tmp_path: Path) -> None:
    out = _bash(
        'unset FC_FROM_BACKEND_DIR; fc_not_applicable_reason v448-tasks && echo "rc=0" || echo "rc=$?"'
    )
    assert "rc=1" in out.stdout


def test_every_mapped_predicate_exists_in_the_python_checker() -> None:
    """The mapping may only name predicates feature-checks.py actually implements."""
    sh = (LIB / "feature-checks.sh").read_text(encoding="utf-8")
    py = (LIB / "feature-checks.py").read_text(encoding="utf-8")
    block = sh[sh.index("declare -A FC_MIGRATION_OF=(") :].split(")", 1)[0]
    names = [line.split("]")[0].split("[")[1] for line in block.splitlines() if "[" in line]
    assert names, "FC_MIGRATION_OF is empty"
    for name in names:
        assert f"'{name}'" in py, (
            f"{name} is mapped to a migration but feature-checks.py has no such predicate"
        )
