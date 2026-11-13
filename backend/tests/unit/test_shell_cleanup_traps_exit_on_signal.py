"""A cleanup trap on INT/TERM must END the script, not just clean up and carry on.

In bash a trap handler that returns hands control back to the interrupted script. A line
like ``trap cleanup EXIT INT TERM`` therefore tears the stack down (or restores the stashed
model set, or the mutated source file) on Ctrl-C and then keeps running the rest of the
script against the state it just removed. Each case below takes the shipped trap line,
arms it in a bash subprocess, delivers SIGINT mid-wait and requires that the script ends
without reaching the code after the wait.
"""

from __future__ import annotations

import re
import signal
import subprocess
from pathlib import Path

import pytest
from fixtures.sigint import assert_signal_is_caught
from fixtures.sigint import with_default_sigint

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRAP_LINE = re.compile(r"^\s*(trap\s.*)$")

_CASES = (
    ("scripts/release/85-smoke.sh", "smoke_exit"),
    ("scripts/release/65-rehearse.sh", "restore_live_stack"),
    ("scripts/e2e/styling-ab.sh", "cleanup"),
    ("scripts/verify-diar-native-e2e.sh", "restore_models"),
)


def _trap_lines(script: str) -> list[str]:
    lines = (_REPO_ROOT / script).read_text(encoding="utf-8").splitlines()
    return [m.group(1) for line in lines if (m := _TRAP_LINE.match(line))]


@pytest.mark.parametrize(("script", "handler"), _CASES)
def test_interrupt_runs_cleanup_then_ends_the_script(script: str, handler: str):
    trap_lines = [ln for ln in _trap_lines(script) if handler in ln or "exit 1" in ln]
    assert any("INT" in ln for ln in trap_lines), f"{script}: no INT trap found; update this test"
    snippet = "\n".join(
        [
            f'{handler}() {{ echo "CLEANUP-RAN"; }}',
            *trap_lines,
            'echo ready; sleep 30 >/dev/null & wait "$!"',
            'echo "SCRIPT-CONTINUED"',
        ]
    )
    proc = subprocess.Popen(  # fixed argv; bash from PATH
        with_default_sigint(["bash", "-c", snippet]),
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    assert_signal_is_caught(proc.pid, signal.SIGINT)
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=30)
    assert "CLEANUP-RAN" in out
    assert "SCRIPT-CONTINUED" not in out, f"{script}: the script kept running after SIGINT"
    assert proc.returncode != 0


def test_mutation_verify_trap_restores_the_file_then_ends(tmp_path: Path):
    source = (_REPO_ROOT / "scripts/run-mutation-tests.sh").read_text(encoding="utf-8")
    all_traps = [ln.strip() for ln in source.splitlines() if ln.strip().startswith("trap ")]
    start = next(i for i, line in enumerate(all_traps) if "cp '$backup'" in line)
    trap_lines = all_traps[start : start + 3]
    assert len(trap_lines) == 3
    backend = tmp_path / "backend"
    backend.mkdir()
    victim = backend / "mod.py"
    victim.write_text("original\n", encoding="utf-8")
    backup = tmp_path / "backup"
    backup.write_text("original\n", encoding="utf-8")
    victim.write_text("MUTATED\n", encoding="utf-8")
    snippet = "\n".join(
        [
            f"backup={backup}; BACKEND={backend}; path=mod.py",
            *trap_lines,
            'echo ready; sleep 30 >/dev/null & wait "$!"',
            'echo "SCRIPT-CONTINUED"',
        ]
    )
    proc = subprocess.Popen(  # fixed argv; bash from PATH
        with_default_sigint(["bash", "-c", snippet]),
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    assert_signal_is_caught(proc.pid, signal.SIGINT)
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=30)
    assert victim.read_text(encoding="utf-8") == "original\n"
    assert "SCRIPT-CONTINUED" not in out
    assert proc.returncode != 0
