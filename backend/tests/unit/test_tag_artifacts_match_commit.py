"""`70-tag.sh` must refuse to tag a commit that test/build/scan/rehearse did not run on.

THE BUG

The ledger records `sha=` for every stage (`release.sh` `ledger_record`) and nothing compared
it. v0.6.0's ledger had no two passing stages on the same commit, and the pipeline would have
tagged HEAD anyway — "built = scanned = rehearsed = tagged" was an aspiration, not a gate.

APPROACH

The `# --- BEGIN artifacts-match-tagged-commit ---` block is extracted from the REAL
`70-tag.sh` and run in a scratch git repo with a hand-written ledger, with `record` and
`fail_out` stubbed to log what the shipped block asked for.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TAG_SH = REPO_ROOT / "scripts" / "release" / "70-tag.sh"
GIT = shutil.which("git") or "git"
VERSION = "v9.9.9"
STAGES = ("test", "build", "scan", "rehearse")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _extract_marked_block(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index(f"# --- BEGIN {name} ---")
    end = text.index(f"# --- END {name} ---", start)
    return text[start : text.index("\n", end) + 1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    for n in (1, 2):
        (r / "f").write_text(str(n))
        _git(r, "add", "-A")
        _git(r, "commit", "-q", "-m", f"c{n}")
    return r


def _ledger(repo: Path, stage: str, *, status: str = "done", sha: str | None = None) -> None:
    steps = repo / ".release" / VERSION / "steps"
    steps.mkdir(parents=True, exist_ok=True)
    short = sha if sha is not None else _git(repo, "rev-parse", "--short", "HEAD")
    (steps / stage).write_text(
        f"status={status}\nwhen=2026-10-30T00:00:00Z\noperator=t\nsha={short}\ndetail=x\n",
        encoding="utf-8",
    )


def _run(repo: Path) -> tuple[int, str, list[list[str]]]:
    log = repo.parent / "records.log"
    snippet = f"""
set -uo pipefail
REPO_ROOT="{repo}"
VERSION="{VERSION}"
RED=''; NC=''
cd "{repo}" || exit 2
record() {{ printf '%s\\t%s\\t%s\\t%s\\n' "$1" "$2" "${{3:-}}" "${{4:-}}" >> "{log}"; }}
fail_out() {{ echo "FAIL_OUT=$1"; exit "$1"; }}
{_extract_marked_block(TAG_SH, "artifacts-match-tagged-commit")}
echo "FELL_THROUGH"
"""
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    records = (
        [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return proc.returncode, proc.stdout + proc.stderr, records


def _all_at_head(repo: Path) -> None:
    for s in STAGES:
        _ledger(repo, s)


@pytest.mark.unit
def test_every_stage_at_head_passes(repo: Path) -> None:
    _all_at_head(repo)

    rc, out, records = _run(repo)

    assert rc == 0 and "FELL_THROUGH" in out, out
    assert [r[:2] for r in records] == [["artifacts-match-tagged-commit", "pass"]], records


@pytest.mark.unit
def test_an_overridden_stage_at_head_counts(repo: Path) -> None:
    _all_at_head(repo)
    _ledger(repo, "scan", status="overridden")

    rc, _, records = _run(repo)

    assert rc == 0 and records[0][1] == "pass", records


@pytest.mark.unit
def test_a_stage_built_on_the_previous_commit_fails_naming_only_it(repo: Path) -> None:
    _all_at_head(repo)
    _ledger(repo, "build", sha=_git(repo, "rev-parse", "--short", "HEAD~1"))

    rc, out, records = _run(repo)

    assert rc == 1 and "FAIL_OUT=1" in out, out
    assert records[0][:2] == ["artifacts-match-tagged-commit", "fail"], records
    assert "build" in records[0][2], records
    for fine in ("test", "scan", "rehearse"):
        assert fine not in records[0][2], records


@pytest.mark.unit
def test_every_stale_stage_is_named_not_just_the_first(repo: Path) -> None:
    _all_at_head(repo)
    old = _git(repo, "rev-parse", "--short", "HEAD~1")
    _ledger(repo, "test", sha=old)
    _ledger(repo, "rehearse", status="failed")

    _, _, records = _run(repo)

    assert "test" in records[0][2] and "rehearse" in records[0][2], records
    assert "build" not in records[0][2], records


@pytest.mark.unit
def test_a_missing_ledger_fails_naming_all_four(repo: Path) -> None:
    rc, _, records = _run(repo)

    assert rc == 1, records
    for s in STAGES:
        assert s in records[0][2], records


@pytest.mark.unit
@pytest.mark.parametrize("status", ["failed", "aborted", "skipped", "pending"])
def test_a_stage_that_did_not_complete_fails_even_at_head(repo: Path, status: str) -> None:
    _all_at_head(repo)
    _ledger(repo, "scan", status=status)

    rc, _, records = _run(repo)

    assert rc == 1 and "scan" in records[0][2], records


@pytest.mark.unit
def test_an_unresolvable_recorded_sha_is_stale(repo: Path) -> None:
    _all_at_head(repo)
    _ledger(repo, "build", sha="unknown")

    rc, _, records = _run(repo)

    assert rc == 1 and "build" in records[0][2], records
