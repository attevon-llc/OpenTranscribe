"""The release-stage staleness gate for visual baselines must actually fire.

A committed screenshot baseline is a picture of what the UI should look like.
When `frontend/src` changes and the baselines are not re-captured, they describe
an app that no longer exists — and nothing reported it: the images went 28
frontend commits stale, six of the ten surfaces were being skipped the whole
time, and the suite stayed green throughout.

`scripts/release/30-verify.sh` now records a `visual-baselines-fresh` criterion
that compares two git facts. `test_release_criteria_wiring.py` already proves the
id is declared and recorded; it cannot prove the comparison WORKS. This does, by
running the real fragment out of the real script against throwaway git repos in
three states — fresh, drifted, re-captured — because a gate that always reports 0
is indistinguishable from a clean tree, which is the exact failure mode the whole
visual suite was suffering from.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_SH = REPO_ROOT / "scripts" / "release" / "30-verify.sh"
BASELINE_DIR = "backend/tests/e2e/__screenshots__"


def _drift_expression() -> str:
    """Pull the real drift computation out of 30-verify.sh.

    Extracted rather than re-implemented: a copy in the test would keep passing
    after the script's own logic was changed or deleted.
    """
    source = VERIFY_SH.read_text(encoding="utf-8")
    match = re.search(r'^\s*(ui_drift="\$\(git log .*?\)"\s*)$', source, re.M)
    assert match, (
        "the `ui_drift=` computation is gone from 30-verify.sh — if the gate was "
        "renamed or removed, update this test rather than deleting it, because a "
        "silently absent staleness gate is what issue #777 was written about"
    )
    return match.group(1).strip()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, path: str, content: str, message: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _measure_drift(repo: Path) -> int:
    """Run the script's own expression in *repo* and return the commit count."""
    script = (
        "set -uo pipefail\n"
        f'cd "{repo}"\n'
        f'baseline_commit="$(git log -1 --format=%H -- {BASELINE_DIR} 2>/dev/null || true)"\n'
        f"{_drift_expression()}\n"
        'printf "%s" "$ui_drift"\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"drift expression failed:\n{proc.stderr}"
    return int(proc.stdout.strip())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo whose only commit refreshes the baselines."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _commit(r, f"{BASELINE_DIR}/gallery-light.png", "img-v1", "baselines")
    return r


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_baselines_newer_than_the_ui_report_no_drift(repo: Path) -> None:
    """The clean state. If this were the only case, a dead gate would pass it."""
    assert _measure_drift(repo) == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ui_commits_after_the_baselines_are_counted(repo: Path) -> None:
    """The must-fire case — the 28-commit rot, in miniature."""
    _commit(repo, "frontend/src/App.svelte", "a", "ui 1")
    _commit(repo, "frontend/src/App.svelte", "ab", "ui 2")
    assert _measure_drift(repo) == 2, (
        "frontend/src moved twice after the baselines were committed and the gate "
        "did not notice. As written it would report a release as verified against "
        "screenshots of a UI that no longer exists."
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_re_capturing_the_baselines_clears_the_drift(repo: Path) -> None:
    """The gate must be self-clearing, or the only way past it is an override."""
    _commit(repo, "frontend/src/App.svelte", "a", "ui 1")
    assert _measure_drift(repo) == 1
    _commit(repo, f"{BASELINE_DIR}/gallery-light.png", "img-v2", "refresh baselines")
    assert _measure_drift(repo) == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_changes_outside_the_ui_do_not_count_as_drift(repo: Path) -> None:
    """Scoped to frontend/src. A backend commit is not a reason to re-shoot."""
    _commit(repo, "backend/app/main.py", "x = 1", "backend change")
    _commit(repo, "docs-site/docs/intro.md", "hi", "docs change")
    assert _measure_drift(repo) == 0, (
        "a non-UI commit counted as baseline drift. The gate would then fire on "
        "commits a re-capture cannot possibly fix, and get overridden by habit."
    )
