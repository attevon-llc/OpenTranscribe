"""Static guards for ``scripts/smoke-container-free.sh``.

The script is only useful while it stays container-free, reports every step, and fails the
run when any step fails. None of those properties announce themselves when they regress: a
step silently dropped or a ``set -e`` abort that hides later results still exits plausibly.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "smoke-container-free.sh"

REQUIRED_STEPS = (
    'run_step "bash -n: release-manifest shell files + opentr.sh"',
    'run_step "import app.main"',
    'run_step "route table dump"',
    'run_step "generate-openapi.py --check"',
    'run_step "generate-pipeline-docs.py --check"',
    'run_step "opentr.sh start dev --dry-run"',
)


def _code_lines() -> list[str]:
    text = SCRIPT.read_text()
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_script_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & 0o111, "script must be executable"
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_step_is_listed() -> None:
    code = "\n".join(_code_lines())
    missing = [s for s in REQUIRED_STEPS if s not in code]
    assert not missing, f"steps missing from the script: {missing}"


def test_never_starts_containers() -> None:
    code = "\n".join(_code_lines())
    assert not re.search(r"docker(\s+compose|-compose)", code), "script must not call docker"
    # opentr.sh may only be invoked as a dry run.
    for line in _code_lines():
        if "./opentr.sh" in line:
            assert "--dry-run" in line, f"opentr.sh invoked without --dry-run: {line!r}"


def test_failing_step_does_not_abort_report_but_fails_exit() -> None:
    code = "\n".join(_code_lines())
    assert "set -euo pipefail" in code
    assert "|| rc=$?" in code, "a failing step must be captured, not abort under set -e"
    # The last command decides the exit status: non-zero iff any step failed.
    assert _code_lines()[-1].strip() == '[ "$FAIL" -eq 0 ]'
