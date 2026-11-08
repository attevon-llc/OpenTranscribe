"""`finish` criterion `demo-upgraded` (issue #628, follow-up P-E).

The demo host pulls GitHub's "Latest" on a timer and records the outcome in
`demo-status.json`. After `finish` has created the release it must see the demo reach that tag:
a demo that is configured but did not upgrade fails the stage (fail closed), and a stage with no
demo configured, or a backport the demo will never follow, records the criterion as waived so
it can never read as a pass.

The `# --- BEGIN demo-upgraded ---` block is extracted from the real `95-finish.sh` and run
with stub `record`/`fail_out`, so the shipped decision logic is what is under test; the status
reader is the real `demo-status-lib.sh`.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_DIR = REPO_ROOT / "scripts" / "release"
FINISH_SH = RELEASE_DIR / "95-finish.sh"
VERSION = "v0.7.0"


def _block() -> str:
    text = FINISH_SH.read_text(encoding="utf-8")
    start = text.index("# --- BEGIN demo-upgraded ---")
    end = text.index("# --- END demo-upgraded ---", start)
    return text[start : text.index("\n", end) + 1]


def _run(tmp_path: Path, *, latest_flag="--latest", env=None, record=None):
    """Run the real block; returns (rc, recorded rows, fail_out argv)."""
    if record is not None:
        (tmp_path / "status.json").write_text(
            record if isinstance(record, str) else json.dumps(record)
        )
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f"""set -uo pipefail
SCRIPT_DIR={RELEASE_DIR}
VERSION={VERSION}
latest_flag='{latest_flag}'
RED=''; NC=''
record() {{ printf 'RECORD|%s|%s|%s|%s\\n' "$1" "$2" "${{3:-}}" "${{5:-}}" >> "{tmp_path}/rows"; }}
fail_out() {{ printf 'FAIL_OUT|%s\\n' "$1" >> "{tmp_path}/rows"; exit "$1"; }}
{_block()}
exit 0
"""
    )
    proc = subprocess.run(
        ["bash", str(harness)],
        env={
            "PATH": "/usr/bin:/bin",
            "DEMO_UPGRADE_WAIT_SECONDS": "0",
            "DEMO_UPGRADE_POLL_SECONDS": "1",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    rows_file = tmp_path / "rows"
    rows = [r.split("|") for r in rows_file.read_text().splitlines()] if rows_file.exists() else []
    return proc.returncode, rows


def _file_env(tmp_path: Path) -> dict[str, str]:
    return {"DEMO_STATUS_FILE": str(tmp_path / "status.json")}


def test_no_demo_configured_is_waived_not_passed(tmp_path):
    rc, rows = _run(tmp_path)

    assert rc == 0
    assert [r[1:3] for r in rows] == [["demo-upgraded", "not-measured"]]
    assert rows[0][4] == "waived"


def test_a_backport_that_is_not_latest_is_waived_even_with_a_demo(tmp_path):
    rc, rows = _run(
        tmp_path,
        latest_flag="--latest=false",
        env=_file_env(tmp_path),
        record={"tag": "v0.6.1", "previous_tag": "v0.6.0", "result": "upgraded"},
    )

    assert rc == 0
    assert rows[0][1:3] == ["demo-upgraded", "not-measured"]
    assert rows[0][4] == "waived"


def test_a_demo_on_this_tag_passes(tmp_path):
    rc, rows = _run(
        tmp_path,
        env=_file_env(tmp_path),
        record={"tag": VERSION, "previous_tag": "v0.6.1", "result": "upgraded"},
    )

    assert rc == 0
    assert [r[1:3] for r in rows] == [["demo-upgraded", "pass"]]


def test_a_demo_still_on_the_old_tag_fails_closed_when_the_wait_ends(tmp_path):
    rc, rows = _run(
        tmp_path,
        env=_file_env(tmp_path),
        record={"tag": "v0.6.1", "previous_tag": "v0.6.0", "result": "upgraded"},
    )

    assert rc == 1
    assert rows[0][1:3] == ["demo-upgraded", "fail"]
    assert rows[-1] == ["FAIL_OUT", "1"]


@pytest.mark.parametrize("result", ["rolled-back", "rollback-failed"])
def test_a_demo_that_rolled_back_from_this_tag_fails_at_once_as_a_rollback(tmp_path, result):
    # A 5 s deadline with a 1 s poll: a reader that mistook the rollback for "still pending"
    # would wait it out and report "had not reached", which the assertions below reject.
    started = time.monotonic()
    rc, rows = _run(
        tmp_path,
        env={
            **_file_env(tmp_path),
            "DEMO_UPGRADE_WAIT_SECONDS": "5",
            "DEMO_UPGRADE_POLL_SECONDS": "1",
        },
        record={"tag": "v0.6.1", "previous_tag": VERSION, "result": result},
    )

    assert time.monotonic() - started < 3
    assert rc == 1
    assert rows[0][1:3] == ["demo-upgraded", "fail"]
    assert "rolled back" in rows[0][3]
    assert rows[-1] == ["FAIL_OUT", "1"]


def test_an_unreadable_record_is_not_measured_and_stops_the_stage(tmp_path):
    rc, rows = _run(tmp_path, env=_file_env(tmp_path), record="{broken")

    assert rc == 1
    assert rows[0][1:3] == ["demo-upgraded", "not-measured"]
    assert rows[0][4] == ""  # not waived: a configured demo that cannot be read proves nothing


def test_a_missing_record_file_is_not_measured_and_stops_the_stage(tmp_path):
    rc, rows = _run(tmp_path, env=_file_env(tmp_path))

    assert rc == 1
    assert rows[0][1:3] == ["demo-upgraded", "not-measured"]


def test_the_command_form_reads_a_remote_record(tmp_path):
    (tmp_path / "status.json").write_text(
        json.dumps({"tag": VERSION, "previous_tag": "v0.6.1", "result": "upgraded"})
    )

    rc, rows = _run(tmp_path, env={"DEMO_STATUS_CMD": f"cat {tmp_path}/status.json"})

    assert rc == 0
    assert rows[0][1:3] == ["demo-upgraded", "pass"]


def test_a_later_upgrade_is_picked_up_while_polling(tmp_path):
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"tag": "v0.6.1", "previous_tag": "v0.6.0", "result": "upgraded"}))
    flip = (
        f"sleep 1; printf '%s' '{json.dumps({'tag': VERSION, 'previous_tag': 'v0.6.1', 'result': 'upgraded'})}'"
        f" > {status}"
    )
    flipper = subprocess.Popen(["bash", "-c", flip])
    try:
        rc, rows = _run(
            tmp_path,
            env={**_file_env(tmp_path), "DEMO_UPGRADE_WAIT_SECONDS": "30"},
        )
    finally:
        flipper.wait(timeout=30)

    assert rc == 0
    assert rows[0][1:3] == ["demo-upgraded", "pass"]


def test_the_criterion_is_declared_blocking_for_the_finish_stage():
    doc = yaml.safe_load((RELEASE_DIR / "release-criteria.yaml").read_text(encoding="utf-8"))
    criteria = {c["id"]: c for c in doc["stages"]["finish"]["criteria"]}

    assert criteria["demo-upgraded"]["severity"] == "blocking"
