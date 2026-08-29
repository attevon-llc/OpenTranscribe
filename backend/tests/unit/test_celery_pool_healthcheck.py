"""Unit tests for scripts/celery_pool_healthcheck.py.

The bug this exists to catch: `celery inspect ping` is answered by a worker's
MainProcess regardless of whether its forked child pool is alive, so a
completely wedged prefork pool (every child dead) still reports "healthy" to
Docker — observed live for 10h46m (69,231 SIGKILLs, zero tasks consumed).
`inspect stats`'s `pool.processes` is the cheap, real signal: it reports the
live child pids, so a wedged pool reports an empty list.

These tests mock `subprocess.run` to feed the exact JSON shapes involved
(live-verified against the real dev stack — see the PR description for the
`docker exec ... celery inspect stats --json` transcript proving the live
positive case, and a nonexistent-worker negative case, both real invocations)
plus the synthetic wedged-pool shape that cannot safely be reproduced against
a live container (SIGKILLing a Celery child is blocked by this repo's own
`permissions.deny`, and for good reason).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scripts.celery_pool_healthcheck import check_pool_has_live_processes  # noqa: E402
from scripts.celery_pool_healthcheck import main  # noqa: E402

HEALTHY_STATS_JSON = (
    '{"cpu-processor@abc123": {"pool": {"implementation": '
    '"celery.concurrency.prefork:TaskPool", "processes": [111, 112, 113]}}}'
)

# The exact shape observed for a wedged prefork pool: MainProcess answers (the
# whole reason `inspect ping` cannot catch this), but every forked child is
# gone, so `processes` reports empty rather than a nonzero pid list.
WEDGED_STATS_JSON = (
    '{"cpu-processor@abc123": {"pool": {"implementation": '
    '"celery.concurrency.prefork:TaskPool", "processes": []}}}'
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["celery"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.unit
class TestCheckPoolHasLiveProcesses:
    def test_healthy_when_pool_reports_live_processes(self):
        with patch("subprocess.run", return_value=_completed(stdout=HEALTHY_STATS_JSON)):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is True
        assert "3 live child process" in reason

    def test_unhealthy_when_the_wedged_pool_shape_is_reported(self):
        """THE regression this script exists for: MainProcess answers, but
        with an empty child-process list — the exact live incident shape.
        """
        with patch("subprocess.run", return_value=_completed(stdout=WEDGED_STATS_JSON)):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "no live child processes" in reason

    def test_unhealthy_when_no_worker_replies(self):
        """Live-verified shape: `celery inspect stats -d <nonexistent>` exits
        69 with empty stdout and 'Error: No nodes replied...' on stderr.
        """
        with patch(
            "subprocess.run",
            return_value=_completed(
                stdout="", stderr="Error: No nodes replied within time constraint", returncode=69
            ),
        ):
            healthy, reason = check_pool_has_live_processes("cpu-processor@nonexistent")

        assert healthy is False
        assert "rc=69" in reason

    def test_unhealthy_when_stdout_is_not_valid_json(self):
        with patch("subprocess.run", return_value=_completed(stdout="not json at all")):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "could not parse" in reason

    def test_unhealthy_when_payload_is_an_empty_object(self):
        with patch("subprocess.run", return_value=_completed(stdout="{}")):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "no worker replied" in reason

    def test_unhealthy_when_the_command_itself_times_out(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="celery", timeout=15),
        ):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "timed out" in reason

    def test_pool_key_missing_entirely_is_unhealthy_not_a_crash(self):
        """A future celery version could drop or rename `pool` — this must
        degrade to unhealthy, never raise (a crashing healthcheck is itself a
        false-unhealthy that could flap a fine worker, but MUST NOT silently
        report healthy on data it cannot interpret).
        """
        with patch(
            "subprocess.run",
            return_value=_completed(stdout='{"cpu-processor@abc123": {}}'),
        ):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False


@pytest.mark.unit
class TestMainExitCode:
    def test_exit_zero_when_healthy(self):
        with patch("subprocess.run", return_value=_completed(stdout=HEALTHY_STATS_JSON)):
            assert main(["celery_pool_healthcheck.py", "cpu-processor@abc123"]) == 0

    def test_exit_one_when_unhealthy(self):
        with patch("subprocess.run", return_value=_completed(stdout=WEDGED_STATS_JSON)):
            assert main(["celery_pool_healthcheck.py", "cpu-processor@abc123"]) == 1

    def test_exit_two_on_bad_usage(self):
        assert main(["celery_pool_healthcheck.py"]) == 2

    def test_invokes_the_real_celery_inspect_stats_command_shape(self):
        """Pin the exact command line against celery's real CLI contract, so a
        typo (`--json` misspelled, `-d` dropped) is caught here instead of at
        3 AM when Docker reports every prefork worker unhealthy at once.
        """
        mock_run = MagicMock(return_value=_completed(stdout=HEALTHY_STATS_JSON))
        with patch("subprocess.run", mock_run):
            check_pool_has_live_processes("cpu-processor@myhost")

        args, kwargs = mock_run.call_args
        command = args[0]
        assert command[:4] == ["celery", "-A", "app.core.celery", "inspect"]
        assert "stats" in command
        assert "-d" in command
        assert "cpu-processor@myhost" in command
        assert "--json" in command
        assert kwargs.get("timeout") is not None
