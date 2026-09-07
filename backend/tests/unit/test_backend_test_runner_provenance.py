"""``run-backend-tests.sh`` must never stamp fresh provenance onto evidence it did not produce.

``--summary`` runs NO tests: it re-reports ``$OT_TEST_OUT_DIR/last.xml``. ``--require-fresh``
exists because ``scripts/test-matrix.sh`` leg 1.2 IS that command, and on 2026-09-06 the
matrix's "backend test summary" leg passed by reading a 986-byte, two-day-old, 5-test artifact
from a different commit.

That fix had a hole one line wide. ``last.meta`` (``sha=``/``epoch=``) was written
**unconditionally** while ``last.xml`` was published only ``[[ -f "$RUN_XML" ]]`` — so a run
whose pytest produced no XML (a usage error, a collection crash, an OOM'd worker) left the
*previous* run's ``last.xml`` carrying **this** run's commit and timestamp. ``--require-fresh``
then verified a claim the script had just forged, and the P0 reopened from the other end.

The second hole was ``--require-fresh`` being honoured only as ``argv[1]``. Written the natural
way round — ``--summary --require-fresh`` — the flag was silently ignored, and a run with an
ignored guard is byte-for-byte indistinguishable from a run that passed it.

These tests drive the **real script** with a stand-in interpreter, because both defects live in
the script's control flow and neither is visible in its output. The stand-in is why they are
unit tests: no pytest, no database, no dev stack — the shim answers ``-c 'import pytest'`` and
then either writes a JUnit XML or does not, which is the only variable that matters here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPO_ROOT / "scripts" / "run-backend-tests.sh"

pytestmark = pytest.mark.skipif(
    not RUNNER.is_file(), reason="scripts/run-backend-tests.sh not present in this checkout"
)

#: A JUnit document the script's own summary reader accepts. Deliberately not empty: a
#: zero-test XML is reported as `EMPTY` and would confuse a pass with a refusal.
_JUNIT = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" '
    'tests="3" time="1.5"/></testsuites>\n'
)


def _head_sha() -> str:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = out.stdout.strip()
    if not sha:
        pytest.skip("not a git checkout — the runner's provenance is git-derived")
    return sha


def _fake_interpreter(tmp_path: Path, *, writes_xml: bool, exit_code: int) -> Path:
    """A stand-in for `OT_TEST_PYTHON`: answers the import probe, then runs "pytest".

    It records the argument list it was handed, so a test can assert what the script
    actually passed through — the only way to see that `--require-fresh` ate a path.
    """
    shim = tmp_path / "fake-python"
    shim.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-c" ]]; then exit 0; fi\n'  # `import pytest` probe
        f'printf "%s\\n" "$@" > "{tmp_path}/argv.txt"\n'
        + (
            'for a in "$@"; do\n'
            '  if [[ "$a" == --junitxml=* ]]; then\n'
            f"    cat > \"${{a#--junitxml=}}\" <<'XML'\n{_JUNIT}XML\n"
            "  fi\n"
            "done\n"
            if writes_xml
            else "# deliberately writes no --junitxml file\n"
        )
        + f"exit {exit_code}\n"
    )
    shim.chmod(0o755)
    return shim


def _run(out_dir: Path, shim: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, OT_TEST_OUT_DIR=str(out_dir))
    if shim is not None:
        env["OT_TEST_PYTHON"] = str(shim)
    return subprocess.run(
        [str(RUNNER), *args], capture_output=True, text=True, env=env, check=False, timeout=120
    )


def _seed_previous_run(out_dir: Path, *, sha: str, epoch: int | None = None) -> None:
    """A complete, correctly-published artifact from some EARLIER run."""
    import time

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "last.xml").write_text(_JUNIT)
    (out_dir / "last.meta").write_text(f"sha={sha}\nepoch={epoch or int(time.time())}\n")


# ---------------------------------------------------------------------------------------
# The evidence and its provenance are published together or not at all.
# ---------------------------------------------------------------------------------------


def test_a_run_that_wrote_no_xml_destroys_the_stale_freshness_claim(tmp_path: Path) -> None:
    """The one-line P0. A failed run must not lend its sha to the previous run's XML."""
    out_dir = tmp_path / "out"
    _seed_previous_run(out_dir, sha="0" * 40)  # a DIFFERENT commit
    shim = _fake_interpreter(tmp_path, writes_xml=False, exit_code=4)

    result = _run(out_dir, shim, "tests/unit")

    assert result.returncode == 4, f"the runner must propagate pytest's status\n{result.stderr}"
    assert (out_dir / "last.xml").exists(), (
        "the previous run's XML is a real artifact and is deliberately kept; only its "
        "freshness claim dies"
    )
    assert not (out_dir / "last.meta").exists(), (
        "last.meta survived a run that produced no XML. If it now carries THIS run's sha, "
        "`--require-fresh --summary` will pass on the previous run's evidence — the exact "
        f"P0 --require-fresh was added to close.\nstderr:\n{result.stderr}"
    )

    # ...and the consequence that actually matters: the freshness gate now refuses.
    reported = _run(out_dir, None, "--require-fresh", "--summary")
    assert reported.returncode != 0, (
        "`--require-fresh --summary` passed over an artifact whose provenance was removed"
    )
    assert "NOT MEASURED" in reported.stderr, reported.stderr


def test_a_successful_run_publishes_the_xml_and_its_provenance_together(tmp_path: Path) -> None:
    """The control. Without it, "removes last.meta" is satisfied by never writing one."""
    out_dir = tmp_path / "out"
    shim = _fake_interpreter(tmp_path, writes_xml=True, exit_code=0)

    result = _run(out_dir, shim, "tests/unit")

    assert result.returncode == 0, result.stderr
    assert (out_dir / "last.xml").exists()
    meta = (out_dir / "last.meta").read_text()
    assert f"sha={_head_sha()}" in meta, meta
    assert "epoch=" in meta, meta

    reported = _run(out_dir, None, "--require-fresh", "--summary")
    assert reported.returncode == 0, (
        f"a freshly published artifact must satisfy --require-fresh\n{reported.stderr}"
    )
    assert "PASS" in reported.stdout, reported.stdout


def test_a_failed_run_does_not_summarise_the_previous_runs_tally(tmp_path: Path) -> None:
    """A number you cannot attribute is not a measurement.

    With a stale `last.xml` on disk and no XML from this run, the end-of-run report used to
    print the PREVIOUS run's `PASS 3 passed` under this run's non-zero exit.
    """
    out_dir = tmp_path / "out"
    _seed_previous_run(out_dir, sha=_head_sha())  # same commit, so age/sha cannot explain it
    shim = _fake_interpreter(tmp_path, writes_xml=False, exit_code=4)

    result = _run(out_dir, shim, "tests/unit")

    assert "PASS  3 passed" not in result.stdout, (
        "the runner reported a tally from an XML this run did not write:\n" + result.stdout
    )
    assert "NOT MEASURED" in result.stderr, result.stderr


# ---------------------------------------------------------------------------------------
# --require-fresh is position-independent, and does not eat a path.
# ---------------------------------------------------------------------------------------


def test_require_fresh_is_honoured_when_it_follows_another_flag(tmp_path: Path) -> None:
    """`--summary --require-fresh` used to parse as a bare `--summary`."""
    out_dir = tmp_path / "out"
    _seed_previous_run(out_dir, sha="1" * 40)  # not HEAD

    trailing = _run(out_dir, None, "--summary", "--require-fresh")
    assert trailing.returncode != 0, (
        "`--summary --require-fresh` re-reported an artifact from a different commit. The "
        "flag was parsed only as argv[1], so writing it second silently disabled it — and "
        "the output is identical either way.\n" + trailing.stdout + trailing.stderr
    )
    assert "NOT MEASURED" in trailing.stderr, trailing.stderr

    # The originally-supported order must keep working.
    leading = _run(out_dir, None, "--require-fresh", "--summary")
    assert leading.returncode != 0, leading.stdout + leading.stderr


def test_require_fresh_does_not_swallow_the_test_path_that_follows_it(tmp_path: Path) -> None:
    """`--require-fresh tests/unit` must run `tests/unit`, not adopt it as a commit sha.

    The old parser took any following token that did not start with `--`, so a path was
    consumed as the sha and the selection silently widened to the WHOLE suite — a 154-second
    run where a 4-second one was asked for, and a summary describing tests nobody selected.
    """
    out_dir = tmp_path / "out"
    shim = _fake_interpreter(tmp_path, writes_xml=True, exit_code=0)

    result = _run(out_dir, shim, "--require-fresh", "tests/unit/test_env_gate.py")
    assert result.returncode == 0, result.stderr

    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert "tests/unit/test_env_gate.py" in argv, (
        f"the path never reached pytest; it was consumed as a sha. argv was: {argv}"
    )
    assert "tests/" not in argv, (
        f"the runner fell back to the default whole-tree selection. argv was: {argv}"
    )


def test_an_explicit_sha_is_still_accepted_in_either_position(tmp_path: Path) -> None:
    """The sha argument survives the tightened parse — it just has to look like a sha."""
    out_dir = tmp_path / "out"
    _seed_previous_run(out_dir, sha="abc1234")

    ok = _run(out_dir, None, "--summary", "--require-fresh", "abc1234")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # Exit 0 alone would also be produced by IGNORING the flag, which is what the old parser
    # did in this position — so assert the freshness check actually ran and said so.
    assert "artifact is from abc1234" in ok.stderr, (
        "--require-fresh passed without evaluating anything; a matching sha and a skipped "
        f"check are indistinguishable by exit code alone.\n{ok.stderr}"
    )

    mismatched = _run(out_dir, None, "--require-fresh", "def5678", "--summary")
    assert mismatched.returncode != 0, mismatched.stdout + mismatched.stderr
    assert "NOT MEASURED" in mismatched.stderr, mismatched.stderr
