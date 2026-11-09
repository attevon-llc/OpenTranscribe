"""``run-backend-tests.sh`` artifacts must be isolated per checkout and per run.

Every run used to write ``$OT_TEST_OUT_DIR/last.log`` / ``last.xml``. Two runs from different git
worktrees (or the same one) overwrote each other, so ``--failures`` / ``--summary`` re-reported
ANOTHER run's results. A result you cannot attribute is not evidence.

These tests drive the real script with a stand-in interpreter (``OT_TEST_PYTHON``) from two
copies of the script, each in a different fake checkout, sharing one output base dir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPO_ROOT / "scripts" / "run-backend-tests.sh"

pytestmark = pytest.mark.skipif(not RUNNER.is_file(), reason="runner not present")

_XML = (
    '<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" failures="1" '
    'skipped="0" tests="2" time="1.0"><testcase classname="mod" name="{name}">'
    '<failure message="boom"/></testcase></testsuite></testsuites>'
)


def _checkout(tmp_path: Path, name: str) -> Path:
    """A fake checkout: a copy of the script tree under its own toplevel path."""
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "backend").mkdir()
    shutil.copy(RUNNER, root / "scripts" / "run-backend-tests.sh")
    shutil.copytree(RUNNER.parent / "lib", root / "scripts" / "lib")
    return root


def _shim(tmp_path: Path, failing_test: str) -> Path:
    shim = tmp_path / f"python-{failing_test}"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-c" ]]; then exit 0; fi\n'
        'for a in "$@"; do\n'
        '  if [[ "$a" == --junitxml=* ]]; then\n'
        f"    cat > \"${{a#--junitxml=}}\" <<'XML'\n{_XML.format(name=failing_test)}\nXML\n"
        "  fi\n"
        "done\n"
        "echo marker-" + failing_test + "\n"
        "exit 1\n"
    )
    shim.chmod(0o755)
    return shim


def _run(root: Path, base: Path, shim: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, OT_TEST_OUT_DIR=str(base))
    if shim:
        env["OT_TEST_PYTHON"] = str(shim)
    return subprocess.run(
        [str(root / "scripts" / "run-backend-tests.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )


def test_two_checkouts_never_see_each_others_results(tmp_path: Path) -> None:
    base = tmp_path / "out"
    a, b = _checkout(tmp_path, "wt-a"), _checkout(tmp_path, "wt-b")
    _run(a, base, _shim(tmp_path, "test_alpha"), "tests/x")
    _run(b, base, _shim(tmp_path, "test_beta"), "tests/x")  # runs AFTER a, same base

    fa = _run(a, base, None, "--failures").stdout
    fb = _run(b, base, None, "--failures").stdout
    assert "test_alpha" in fa and "test_beta" not in fa, fa
    assert "test_beta" in fb and "test_alpha" not in fb, fb

    log_a = Path(_run(a, base, None, "--log").stdout.strip())
    log_b = Path(_run(b, base, None, "--log").stdout.strip())
    assert log_a.parent != log_b.parent
    assert "marker-test_alpha" in log_a.read_text()
    assert "marker-test_beta" in log_b.read_text()


def test_printed_path_is_the_per_run_log_and_survives_later_runs(tmp_path: Path) -> None:
    base = tmp_path / "out"
    a = _checkout(tmp_path, "wt-a")
    first = _run(a, base, _shim(tmp_path, "test_one"), "tests/x")
    printed = next(
        line.split("Full output: ")[1].split()[0].split("\x1b")[0]
        for line in first.stdout.splitlines()
        if "Full output: " in line
    )
    assert "/runs/" in printed
    _run(a, base, _shim(tmp_path, "test_two"), "tests/x")
    assert "marker-test_one" in Path(printed).read_text(), (
        "a later run clobbered an earlier run's log"
    )
    assert "test_two" in _run(a, base, None, "--failures").stdout


def test_only_newest_runs_of_this_checkout_are_kept(tmp_path: Path) -> None:
    base = tmp_path / "out"
    a, b = _checkout(tmp_path, "wt-a"), _checkout(tmp_path, "wt-b")
    _run(b, base, _shim(tmp_path, "test_b"), "tests/x")
    for _ in range(4):
        _run(a, base, _shim(tmp_path, "test_a"), "tests/x")
    env_keep = dict(os.environ, OT_TEST_KEEP_RUNS="2")
    for _ in range(3):
        subprocess.run(
            [str(a / "scripts" / "run-backend-tests.sh"), "tests/x"],
            env=dict(
                env_keep, OT_TEST_OUT_DIR=str(base), OT_TEST_PYTHON=str(_shim(tmp_path, "test_a"))
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    runs_a = Path(_run(a, base, None, "--log").stdout.strip()).parent / "runs"
    runs_b = Path(_run(b, base, None, "--log").stdout.strip()).parent / "runs"
    assert len(list(runs_a.glob("*.log"))) == 2
    assert len(list(runs_b.glob("*.log"))) == 1, "pruning touched another checkout"
