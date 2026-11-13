"""build_snapshot.sh must bring a restored snapshot to the head schema before it seeds.

``pg_restore --clean`` replaces the schema the fresh stack migrated with the snapshot's older one,
so without a migrate step the seed and every later request run against missing columns (the
2026-11-13 load run answered 500 ``UndefinedColumn`` to 36% of the smoke). The migrate-and-verify
step now lives in ``scripts/lib/stack_target.py`` (``schema-head+migrate``, exit 22 when the schema
is still behind), so this test drives the script against a stub ``stack-target.sh`` that records
every requirement and a stub ``docker``/``seed-query-review.sh`` that record the order of events.
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
"""

STACK_TARGET = """
st_target() { echo "st_target $*" >> "$STUB_LOG"; }
st_container() { echo cid; }
st_require() {
  echo "st_require $*" >> "$STUB_LOG"
  if [[ "$*" == *schema-head+migrate* && -n "${STUB_SCHEMA_RC:-}" && -f "$STUB_RESTORED" ]]; then
    echo "stack-target: FAIL SCHEMA(22)" >&2
    return "$STUB_SCHEMA_RC"
  fi
}
"""


def _tree(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    tools = tmp_path / "repo" / "scripts" / "perf" / "load" / "tools"
    tools.mkdir(parents=True)
    shutil.copy(SCRIPT, tools / "build_snapshot.sh")
    seed = tmp_path / "repo" / "scripts" / "seed-query-review.sh"
    seed.write_text('#!/bin/bash\necho "SEED $*" >> "$STUB_LOG"\n')
    lib = tmp_path / "repo" / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "stack-target.sh").write_text(STACK_TARGET)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        DOCKER.replace(
            'echo "docker',
            'case "$*" in *pg_restore*) touch "$STUB_RESTORED";; esac\necho "docker',
            1,
        )
    )
    for f in (seed, bin_dir / "docker"):
        f.chmod(0o755)
    old = tmp_path / "old.dump"
    old.write_bytes(b"dump")
    (tmp_path / "old.dump.sha256").write_text(
        subprocess.check_output(["sha256sum", "old.dump"], cwd=tmp_path, text=True)
    )
    log = tmp_path / "log"
    env = {
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log),
        "STUB_RESTORED": str(tmp_path / "restored"),
    }  # fmt: skip
    return tools / "build_snapshot.sh", log, env


def _run(tmp_path: Path, schema_rc: str = "") -> tuple[subprocess.CompletedProcess, list[str]]:
    script, log, env = _tree(tmp_path)
    env["STUB_SCHEMA_RC"] = schema_rc
    done = subprocess.run(
        [str(script), "t1", str(tmp_path / "out.dump"), "--from-snapshot", str(tmp_path / "old.dump")],
        env=env, capture_output=True, text=True, cwd=tmp_path, check=False,
    )  # fmt: skip
    return done, log.read_text().splitlines()


def test_migrate_runs_between_restore_and_seed(tmp_path):
    done, calls = _run(tmp_path)

    assert done.returncode == 0, done.stderr
    restore = next(i for i, c in enumerate(calls) if "pg_restore" in c)
    migrate = max(i for i, c in enumerate(calls) if "schema-head+migrate" in c)
    seed = next(i for i, c in enumerate(calls) if c.startswith("SEED"))
    assert restore < migrate < seed


def test_a_database_not_at_head_fails_before_the_seed_with_the_library_code(tmp_path):
    done, calls = _run(tmp_path, schema_rc="22")

    assert done.returncode == 22
    assert not any(c.startswith("SEED") for c in calls)


def test_statement_statistics_are_prepared_before_seeding(tmp_path):
    _, calls = _run(tmp_path)

    prepare = next(i for i, c in enumerate(calls) if "ext:pg_stat_statements+prepare" in c)
    seed = next(i for i, c in enumerate(calls) if c.startswith("SEED"))
    assert prepare < seed


@pytest.mark.parametrize("flag", ["--activity-only"])
def test_seed_still_receives_activity_only_after_migrate(tmp_path, flag):
    _, calls = _run(tmp_path)

    assert any(c.startswith("SEED") and flag in c for c in calls)


def test_run_profile_prepares_statement_statistics_itself_and_never_in_a_shipped_compose_file():
    run_profile = (SCRIPT.parent / "run_profile.sh").read_text()
    readme = (ROOT / "scripts" / "perf" / "load" / "README.md").read_text()

    assert "ext:pg_stat_statements+prepare" in run_profile
    assert "set -euo pipefail" in run_profile
    assert "stack_target.py prepare --fresh" in readme
    core = (ROOT / "scripts" / "lib" / "stack_core.py").read_text()
    assert "ALTER SYSTEM SET shared_preload_libraries = pg_stat_statements" in core
    for compose in ROOT.glob("docker-compose*.yml"):
        assert "shared_preload_libraries" not in compose.read_text(), compose.name
