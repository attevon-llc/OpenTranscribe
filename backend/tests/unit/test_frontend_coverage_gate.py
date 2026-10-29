"""The frontend coverage floors must exist, only ratchet upward, and be evaluated in CI.

`frontend/vitest.config.ts` carries `coverage.thresholds`, but vitest only evaluates them under
`--coverage`. When CI ran plain `npm run test` the floors were decorative and sat 40 points
below measured coverage. Three things are pinned here:

  * all four metrics have a threshold;
  * each is >= the value in FLOOR_TABLE below (raise the table with the config, never lower it);
  * the frontend CI job runs the suite with coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
VITEST_CONFIG = REPO / "frontend" / "vitest.config.ts"
CI_WORKFLOW = REPO / ".github" / "workflows" / "pre-commit.yml"
FRONTEND_JOB = "frontend-tests"

# Ratchet table: the lowest floor each metric may ever have. Raise together with the config.
FLOOR_TABLE: dict[str, float] = {
    "lines": 64.2,
    "statements": 62.7,
    "functions": 62.7,
    "branches": 57.3,
}


def read_thresholds(config_text: str) -> dict[str, float]:
    """Extract the `thresholds: { ... }` block of the coverage config as metric -> value."""
    block = re.search(r"^\s*thresholds:\s*\{(?P<body>[^}]*)\}", config_text, re.MULTILINE)
    assert block, "coverage.thresholds block not found in vitest.config.ts"
    # Strip comments so a commented-out value cannot be read as live.
    body = re.sub(r"//[^\n]*", "", block.group("body"))
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"(\w+)\s*:\s*([0-9.]+)", body)}


def frontend_run_commands(workflow_text: str) -> list[str]:
    """All `run:` strings of the frontend job's steps."""
    jobs = yaml.safe_load(workflow_text)["jobs"]
    assert FRONTEND_JOB in jobs, f"job {FRONTEND_JOB!r} missing from pre-commit.yml"
    return [s["run"] for s in jobs[FRONTEND_JOB]["steps"] if "run" in s]


def test_all_four_metrics_have_a_threshold() -> None:
    thresholds = read_thresholds(VITEST_CONFIG.read_text())
    assert set(thresholds) == set(FLOOR_TABLE)


@pytest.mark.parametrize("metric", sorted(FLOOR_TABLE))
def test_threshold_has_not_been_lowered(metric: str) -> None:
    thresholds = read_thresholds(VITEST_CONFIG.read_text())
    assert thresholds[metric] >= FLOOR_TABLE[metric], (
        f"coverage.thresholds.{metric} = {thresholds[metric]} is below the ratchet floor "
        f"{FLOOR_TABLE[metric]}; floors only go up"
    )


def test_ci_frontend_job_runs_coverage() -> None:
    commands = frontend_run_commands(CI_WORKFLOW.read_text())
    assert any(re.search(r"\bnpm run test:coverage\b", c) for c in commands), (
        "the frontend CI job must run `npm run test:coverage`, or the thresholds are never evaluated"
    )


def test_ci_syncs_sveltekit_before_coverage() -> None:
    # `test:coverage` has no pretest hook, so without `svelte-kit sync` vitest cannot resolve
    # .svelte-kit/tsconfig.json and dies at startup.
    commands = frontend_run_commands(CI_WORKFLOW.read_text())
    joined = "\n".join(commands)
    assert re.search(r"svelte-kit sync[\s\S]*npm run test:coverage", joined)
