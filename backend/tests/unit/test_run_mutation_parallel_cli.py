"""`run-mutation-tests.sh --parallel` argument handling and dry run (EFFA, design 2a)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-mutation-tests.sh"


def _run(out_dir: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(out_dir),
        "OT_MUTATION_OUT_DIR": str(out_dir / "out"),
    }
    env.update(env_extra or {})
    return subprocess.run(
        [str(_SCRIPT), *args], capture_output=True, text=True, timeout=120, env=env
    )


def test_dry_run_prints_the_per_module_isolation_and_touches_no_docker(tmp_path):
    r = _run(tmp_path, "--parallel", "--modules", "spans,session", "--dry-run", "--jobs", "2",
             env_extra={"OT_MUTATION_WORKTREE_ROOT": str(tmp_path / "wt"), "OT_MUTATION_CPU_BUDGET": "20"})  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "module session:" in out and "module spans:" in out
    assert "max-children=10" in out
    assert "POSTGRES_DB=ot_mut_spans" in out and "POSTGRES_DB=ot_mut_session" in out
    assert "-p no:testmon" in out
    assert "REDIS_URL=redis://***@" in out
    assert not (tmp_path / "wt").exists(), "dry run must not create worktrees"


def test_an_unknown_module_is_misuse(tmp_path):
    r = _run(tmp_path, "--parallel", "--modules", "nope", "--dry-run")
    assert r.returncode == 2


def test_a_bad_jobs_value_is_misuse(tmp_path):
    assert _run(tmp_path, "--parallel", "--jobs", "0", "--dry-run").returncode == 2


def test_the_dirty_file_check_names_a_modified_module_file():
    # The real checkout, one tracked MODULE_PATH file made dirty by a scratch worktree-less edit
    # would race other writers, so this asserts the helper on a throwaway repo instead.
    helper = (
        "set -u; RED=;GREEN=;YELLOW=;BLUE=;NC=; declare -A MODULE_PATH=([k]=app/m.py) MODULE_TESTS=([k]='tests/t.py');"
        f"source {_SCRIPT.parent}/lib/mutation-stack.sh;"
        "d=$(mktemp -d); git -C $d init -q; mkdir -p $d/backend/app $d/backend/tests;"
        "echo a >$d/backend/app/m.py; echo a >$d/backend/tests/t.py; git -C $d add -A;"
        "git -C $d -c user.email=a@b -c user.name=n commit -qm x; echo b >>$d/backend/app/m.py;"
        "REPO_ROOT=$d; mutation_parallel_dirty_files k"
    )
    r = subprocess.run(["bash", "-c", helper], capture_output=True, text=True, timeout=60)
    assert "backend/app/m.py" in r.stdout, r.stderr
