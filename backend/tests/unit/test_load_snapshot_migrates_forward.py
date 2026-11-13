"""build_snapshot.sh must bring a restored snapshot to the head schema before it seeds.

``pg_restore --clean`` replaces the schema the fresh stack migrated with the snapshot's older one,
so without a migrate step the seed and every later request run against missing columns (the
2026-11-13 load run answered 500 ``UndefinedColumn`` to 36% of the smoke). The script is copied into
a stub tree whose ``docker`` records its calls and whose ``seed-query-review.sh`` records the seed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "perf" / "load" / "tools" / "build_snapshot.sh"

DOCKER = """#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  ps) echo cid ;;
  exec)
    case "$*" in
      *"alembic_version"*) echo "$STUB_REVISION" ;;
    esac ;;
esac
"""


def _tree(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    tools = tmp_path / "repo" / "scripts" / "perf" / "load" / "tools"
    tools.mkdir(parents=True)
    shutil.copy(SCRIPT, tools / "build_snapshot.sh")
    seed = tmp_path / "repo" / "scripts" / "seed-query-review.sh"
    seed.write_text('#!/bin/bash\necho "SEED $*" >> "$STUB_LOG"\n')
    lib = tmp_path / "repo" / "scripts" / "release-tests" / "lib"
    lib.mkdir(parents=True)
    (lib / "alembic-head.py").write_text("print('v999_head')\n")
    (tmp_path / "repo" / "backend").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    for f in (seed, bin_dir / "docker"):
        f.chmod(0o755)
    old = tmp_path / "old.dump"
    old.write_bytes(b"dump")
    (tmp_path / "old.dump.sha256").write_text(
        subprocess.check_output(["sha256sum", "old.dump"], cwd=tmp_path, text=True)
    )
    log = tmp_path / "log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log)}
    return tools / "build_snapshot.sh", log, env


def _run(tmp_path: Path, revision: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    script, log, env = _tree(tmp_path)
    env["STUB_REVISION"] = revision
    done = subprocess.run(
        [str(script), "t1", str(tmp_path / "out.dump"), "--from-snapshot", str(tmp_path / "old.dump")],
        env=env, capture_output=True, text=True, cwd=tmp_path, check=False,
    )  # fmt: skip
    return done, log.read_text().splitlines()


def test_migrate_runs_between_restore_and_seed(tmp_path):
    done, calls = _run(tmp_path, "v999_head")

    assert done.returncode == 0, done.stderr
    restore = next(i for i, c in enumerate(calls) if "pg_restore" in c)
    migrate = next(i for i, c in enumerate(calls) if "app.startup.migrate" in c)
    seed = next(i for i, c in enumerate(calls) if c.startswith("SEED"))
    assert restore < migrate < seed


def test_a_database_not_at_head_fails_before_the_seed(tmp_path):
    done, calls = _run(tmp_path, "v468_add_organization_owner")

    assert done.returncode != 0
    assert "v468_add_organization_owner" in done.stderr
    assert not any(c.startswith("SEED") for c in calls)


@pytest.mark.parametrize("flag", ["--activity-only"])
def test_seed_still_receives_activity_only_after_migrate(tmp_path, flag):
    _, calls = _run(tmp_path, "v999_head")

    assert any(c.startswith("SEED") and flag in c for c in calls)


def test_statement_statistics_are_preloaded_per_stack_never_in_a_shipped_compose_file():
    run_profile = (SCRIPT.parent / "run_profile.sh").read_text()
    readme = (ROOT / "scripts" / "perf" / "load" / "README.md").read_text()

    assert "shared_preload_libraries" in run_profile
    assert "create extension if not exists pg_stat_statements" in run_profile
    assert "ALTER SYSTEM SET" in readme
    for compose in ROOT.glob("docker-compose*.yml"):
        assert "shared_preload_libraries" not in compose.read_text(), compose.name
