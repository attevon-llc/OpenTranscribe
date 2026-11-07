"""``scripts/run-summary-eval.sh``: argument refusals, GPU 0 refusal, and a faithful dry-run.

The script starts a stack and a GPU-backed model, so none of that can run here. What can be
pinned is everything it decides BEFORE touching either: it refuses unsafe or incomplete
invocations, and ``--dry-run`` prints every command and executes none of them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-summary-eval.sh"
ARMS = ["--arm", "old=@module", "--arm", "new=/arms/new.json"]


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "SUMMARY_EVAL_WORK_DIR": str(tmp_path / "work"),
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


def test_both_arms_are_required(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "x", "--arm", "old=@module", "--dry-run")
    assert result.returncode == 2
    assert "both required" in result.stderr


def test_a_run_needs_an_isolated_stack_or_an_explicit_server(tmp_path: Path) -> None:
    result = _run(tmp_path, *ARMS, "--dry-run")
    assert result.returncode == 2
    assert "--fresh NAME" in result.stderr


def test_an_unknown_option_is_a_misuse_error(tmp_path: Path) -> None:
    assert _run(tmp_path, "--bogus").returncode == 2


def test_a_non_positive_concurrency_is_a_misuse_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "x", *ARMS, "--concurrency", "0", "--dry-run")
    assert result.returncode == 2
    assert "--concurrency" in result.stderr


def test_gpu_zero_is_refused_even_for_a_dry_run(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "x", *ARMS, "--dry-run", LLM_TEST_GPU_DEVICE_ID="0")
    assert result.returncode == 2
    assert "GPU 0" in result.stderr
    assert "start dev" not in result.stdout  # nothing past the GPU check was even planned


def test_dry_run_prints_the_whole_sequence_and_creates_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, "--fresh", "sumeval", *ARMS, "--dry-run")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    steps = [ln for ln in out.splitlines() if ln.startswith("==> step")]
    assert [s.split(":")[0] for s in steps] == [f"==> step {n}" for n in range(1, 6)]
    for fragment in (
        "start dev --fresh sumeval",
        "--with-llm-test",
        "plan-slices",
        "benchmark_summary.py run --inputs smoke",
        "benchmark_summary.py project",
        "benchmark_summary.py run --inputs gate --runs 5",
        "benchmark_summary.py replay-merge",
        "benchmark_summary.py judge",
        "benchmark_summary.py report",
    ):
        assert fragment in out, fragment
    assert not (tmp_path / "work").exists()  # a dry-run writes no run directory


def test_an_external_server_skips_gpu_and_stack_management(tmp_path: Path) -> None:
    result = _run(tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "GPU check skipped" in result.stdout
    assert "start dev" not in result.stdout
    assert "--fresh" not in result.stdout


@pytest.mark.parametrize(("inputs", "present"), [("smoke", False), ("gate", True)])
def test_smoke_inputs_run_no_controls_replay_or_judge(
    tmp_path: Path, inputs: str, present: bool
) -> None:
    result = _run(
        tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--inputs", inputs, "--dry-run"
    )
    assert result.returncode == 0, result.stderr
    assert ("benchmark_summary.py replay-merge" in result.stdout) is present
    assert ("benchmark_summary.py judge" in result.stdout) is present


def test_the_remote_leg_is_opt_in_and_runs_the_anthropic_dialect_on_the_smoke_input(
    tmp_path: Path,
) -> None:
    plain = _run(tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--dry-run")
    assert "--provider anthropic" not in plain.stdout
    remote = _run(
        tmp_path, "--llm-base-url", "http://localhost:1/v1", *ARMS, "--remote-leg", "--dry-run"
    )
    assert remote.returncode == 0, remote.stderr
    line = next(ln for ln in remote.stdout.splitlines() if "--provider anthropic" in ln)
    assert "--inputs smoke" in line and "--model claude-haiku-4-5" in line
    assert "ANTHROPIC_API_KEY" not in remote.stdout  # the key is never echoed
