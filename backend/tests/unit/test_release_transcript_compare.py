"""``scripts/release-tests/lib/transcript-compare.py``: the upgrade rehearsal's transcript check.

The rehearsal judges "the transcript survived the upgrade" (prefix mode) and "the rollback
restored it exactly" (exact mode) through this script's exit code, so each verdict needs a
case that must pass and a case that must fail.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "release-tests"
    / "lib"
    / "transcript-compare.py"
)

BASE = [{"text": "hello", "start": 0.0}, {"text": "world", "start": 1.5}]


def _run(tmp_path: Path, mode: str, pre: Any, post: Any) -> subprocess.CompletedProcess[str]:
    pre_path, post_path = tmp_path / "pre.json", tmp_path / "post.json"
    for path, body in ((pre_path, pre), (post_path, post)):
        if body is not None:
            path.write_text(body if isinstance(body, str) else json.dumps(body))
    return subprocess.run(  # fixed argv: this interpreter running the repo script
        [sys.executable, str(SCRIPT), mode, str(pre_path), str(post_path)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("mode", ["prefix", "exact"])
def test_identical_transcripts_hold_in_both_modes(tmp_path: Path, mode: str) -> None:
    result = _run(tmp_path, mode, {"segments": BASE}, {"segments": BASE})
    assert result.returncode == 0
    assert result.stdout.strip() == "pre=2 post=2"


def test_prefix_accepts_added_segments_but_exact_does_not(tmp_path: Path) -> None:
    grown = {"transcript_segments": [*BASE, {"text": "again", "start": 3.0}]}
    assert _run(tmp_path, "prefix", {"segments": BASE}, grown).returncode == 0
    assert _run(tmp_path, "exact", {"segments": BASE}, grown).returncode == 1


@pytest.mark.parametrize("mode", ["prefix", "exact"])
def test_changed_text_lost_segment_or_moved_start_fails(tmp_path: Path, mode: str) -> None:
    changed = [{"text": "hello", "start": 0.0}, {"text": "WORLD", "start": 1.5}]
    moved = [{"text": "hello", "start": 0.0}, {"text": "world", "start": 1.6}]
    for post in (changed, moved, BASE[:1]):
        assert _run(tmp_path, mode, {"segments": BASE}, {"segments": post}).returncode == 1


def test_start_within_tolerance_still_matches(tmp_path: Path) -> None:
    nudged = [{"text": "hello", "start": 0.005}, {"text": "world", "start": 1.5}]
    assert _run(tmp_path, "exact", {"segments": BASE}, {"segments": nudged}).returncode == 0


def test_missing_or_corrupt_file_fails_rather_than_passing(tmp_path: Path) -> None:
    for pre, post in (({"segments": BASE}, None), ("{not json", {"segments": BASE})):
        result = _run(tmp_path, "prefix", pre, post)
        assert result.returncode == 1
        assert result.stderr == ""  # a clean verdict, not a crash the shell reads as a failure


def test_misuse_is_exit_2(tmp_path: Path) -> None:
    assert _run(tmp_path, "loose", {"segments": BASE}, {"segments": BASE}).returncode == 2
