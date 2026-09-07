"""The CI FIPS step and the gate's FIPS phase must run the SAME suites.

``scripts/run-integration-tests.sh``'s ``FIPS_MODE_SUITES`` and the ``FIPS_MODE: 'true'`` step
in ``.github/workflows/pre-commit.yml`` are two copies of one list, and copies drift silently.
They already had:

* CI listed ``tests/test_admin_endpoints.py``, which the gate does not — and which the job's
  own ``pytest tests/`` step had already run, so it bought a duplicate EXECUTION and no test;
* CI exported seven ``RUN_*_TESTS`` variables no test reads. Commit ``b75ecc5b`` deleted the
  local half and missed the CI twin, so a step named "Run security-gated suites" went on
  gating nothing in the one place every contributor looks at on a PR.

Neither divergence is visible at runtime: both sides pass. The only observable is that the two
files disagree, so a static comparison is the only instrument that can see it.

⚠️ Direction of the fix, when this fails: make the two lists equal. Do NOT delete the
assertion, and do not "fix" it by copying whichever list happens to be shorter — check first
whether the file that appears on one side only is covered by CI's unconditional
``pytest tests/`` step (in which case CI must drop it) or is a real suite the gate lost.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "scripts" / "run-integration-tests.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pre-commit.yml"
BACKEND = REPO_ROOT / "backend"

pytestmark = pytest.mark.skipif(
    not GATE.is_file() or not WORKFLOW.is_file(),
    reason="run-integration-tests.sh / pre-commit.yml not present in this checkout",
)

_TEST_FILE = re.compile(r"tests/[\w/]+\.py")


def _gate_fips_suites() -> list[str]:
    match = re.search(r"FIPS_MODE_SUITES=\((.*?)\)", GATE.read_text(encoding="utf-8"), re.S)
    assert match, (
        "run-integration-tests.sh no longer declares FIPS_MODE_SUITES. If the FIPS phase was "
        "renamed, point this module at the new name rather than deleting the comparison — CI "
        "still runs its own copy of that list."
    )
    return sorted(set(_TEST_FILE.findall(match.group(1))))


def _ci_fips_suites() -> list[str]:
    """The pytest file list of the workflow step that sets ``FIPS_MODE``."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    fips = [i for i, line in enumerate(lines) if re.match(r"\s*FIPS_MODE:\s*['\"]?true", line)]
    assert len(fips) == 1, (
        f"expected exactly one workflow step to set FIPS_MODE, found {len(fips)}. Two FIPS "
        "steps is the shape that was just deleted: one pass under the flag is a real claim, "
        "a second is a re-run of the Unit/API suite."
    )
    run = next(
        (i for i in range(fips[0], len(lines)) if re.match(r"\s*run:\s*[>|]-?\s*$", lines[i])),
        None,
    )
    assert run is not None, "the FIPS step has no folded `run:` block"
    indent = len(lines[run]) - len(lines[run].lstrip())
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body.append(line)
    return sorted(set(_TEST_FILE.findall(" ".join(body))))


def test_the_two_extractions_find_a_real_list():
    """Non-vacuity: two empty lists compare equal, and would pass everything below."""
    gate, ci = _gate_fips_suites(), _ci_fips_suites()
    assert len(gate) >= 5, f"only parsed {gate} out of FIPS_MODE_SUITES"
    assert len(ci) >= 5, f"only parsed {ci} out of the workflow's FIPS step"


def test_ci_runs_exactly_the_suites_the_gate_runs_under_fips():
    gate, ci = _gate_fips_suites(), _ci_fips_suites()
    assert ci == gate, (
        "the CI FIPS step and run-integration-tests.sh's FIPS phase have drifted.\n"
        f"  only in CI:   {sorted(set(ci) - set(gate))}\n"
        f"  only in gate: {sorted(set(gate) - set(ci))}\n"
        "Make them equal. A file present only in CI is very likely already covered by the "
        "job's unconditional `pytest tests/` step, which makes it a duplicate execution "
        "rather than coverage."
    )


def test_every_named_suite_exists():
    """A rename on one side is otherwise a phase that silently selects nothing."""
    missing = sorted(f for f in _gate_fips_suites() if not (BACKEND / f).is_file())
    assert not missing, (
        f"the FIPS phase names suites that do not exist: {missing}. pytest exits 4 on a "
        "missing path, but the same list in CI would fail the job for a reason that reads "
        "like an infrastructure problem."
    )
