"""Unit tests for scripts/celery_pool_healthcheck.py.

The bug this exists to catch: `celery inspect ping` is answered by a worker's
MainProcess regardless of whether its forked child pool is alive, so a wedged
prefork pool still reports "healthy" to Docker — observed live for 10h46m
(69,231 SIGKILLs, zero tasks consumed, issue #631).

Two distinct pool failures are covered, and the second is the one the incident
actually had:

* **Dead pool** — `pool.processes` empty.
* **Respawn storm** — `pool.processes` NON-empty and churning, with the
  accepted-task counter frozen. `billiard.pool.Pool._create_worker_process`
  appends a child to `self._pool` *before* `w.start()`, so a pool that forks and
  SIGKILLs children continuously always has pids to report. A non-empty check
  calls that healthy.

  This is not a paper claim: it was reproduced live against a real prefork
  worker in a `--fresh` stack (huggingface.co blackholed in the container's
  `/etc/hosts`, `celery control pool_grow` to force a respawn while the hub was
  running). At 54 SIGKILLs over 117s the pool reported
  `processes=[1878, 1881]`, `total={}` — the pre-fix script exited 0, the
  current one exits 1 on the second probe. Nothing was killed by hand; every
  SIGKILL was celery's own `asynpool.verify_process_alive`.

These tests mock `subprocess.run` to feed the exact JSON shapes involved,
including the live-captured storm shape above.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import yaml

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scripts import celery_pool_healthcheck  # noqa: E402
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


def _stats_json(worker: str, pids: list[int], total: dict | None = None) -> str:
    """One `celery inspect stats --json` payload, in the real reply shape."""
    return json.dumps(
        {
            worker: {
                "total": {} if total is None else total,
                "pool": {
                    "implementation": "celery.concurrency.prefork:TaskPool",
                    "processes": pids,
                },
            }
        }
    )


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["celery"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(autouse=True)
def _isolated_sample_dir(monkeypatch, tmp_path):
    """Keep each test's persisted prior sample out of the real /tmp and out of
    every other test's way — the storm check is stateful across probes."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


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

    def test_asks_for_a_reply_window_wider_than_celerys_one_second_default(self):
        """`inspect stats` is answered by the MainProcess event loop; celery's own
        1.0s default (`celery/bin/control.py`) misses it on a busy worker and flips
        the container unhealthy for a reason unrelated to pool health.
        """
        mock_run = MagicMock(return_value=_completed(stdout=HEALTHY_STATS_JSON))
        with patch("subprocess.run", mock_run):
            check_pool_has_live_processes("cpu-processor@myhost")

        command = mock_run.call_args[0][0]
        assert "--timeout" in command
        reply_window = float(command[command.index("--timeout") + 1])
        assert reply_window > 1.0

    def test_the_probe_skips_the_ml_preamble_it_has_no_use_for(self):
        """The probe broadcasts and reads a reply; it never runs a task. Importing
        torch for that cost 7.0-7.7s of a 10s compose healthcheck timeout, measured
        on a live worker, five containers deep, every 30s.
        """
        mock_run = MagicMock(return_value=_completed(stdout=HEALTHY_STATS_JSON))
        with patch("subprocess.run", mock_run):
            check_pool_has_live_processes("cpu-processor@myhost")

        env = mock_run.call_args.kwargs.get("env") or {}
        assert env.get("SKIP_CELERY") == "true"
        assert "PATH" in env, "the child must inherit the environment, not replace it"


@pytest.mark.unit
class TestRespawnStormDetection:
    """The failure `pool.processes`-non-empty cannot see (issue #631)."""

    def test_a_churning_pool_with_no_task_progress_is_unhealthy(self):
        """THE incident shape, live-captured: every pid replaced between probes,
        `total` frozen. The pre-fix script exits 0 on both of these payloads.
        """
        worker = "cpu-processor@storm"
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json(worker, [11, 12]))):
            first_healthy, _ = check_pool_has_live_processes(worker)
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json(worker, [98, 99]))):
            second_healthy, reason = check_pool_has_live_processes(worker)

        assert first_healthy is True, "the first probe has nothing to compare against"
        assert second_healthy is False
        assert "respawn storm" in reason
        assert "[98, 99]" in reason

    def test_a_fully_recycled_pool_that_is_accepting_tasks_is_healthy(self):
        """`--max-tasks-per-child` replaces pids too — but only *because* tasks
        completed, so the counter moves. Without this half, ordinary recycling under
        load would be reported as a storm.
        """
        worker = "cpu-processor@busy"
        with patch(
            "subprocess.run",
            return_value=_completed(stdout=_stats_json(worker, [11, 12], {"work": 40})),
        ):
            check_pool_has_live_processes(worker)
        with patch(
            "subprocess.run",
            return_value=_completed(stdout=_stats_json(worker, [98, 99], {"work": 81})),
        ):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "accepting tasks" in reason

    def test_a_pool_that_keeps_any_child_across_probes_is_healthy(self):
        worker = "cpu-processor@steady"
        with patch(
            "subprocess.run", return_value=_completed(stdout=_stats_json(worker, [11, 12, 13]))
        ):
            check_pool_has_live_processes(worker)
        with patch(
            "subprocess.run", return_value=_completed(stdout=_stats_json(worker, [11, 12, 99]))
        ):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "carried over" in reason

    def test_the_first_probe_after_a_restart_defers_rather_than_guesses(self):
        worker = "cpu-processor@fresh"
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json(worker, [11, 12]))):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "storm check deferred" in reason

    def test_a_stale_prior_sample_is_not_compared_against(self, monkeypatch):
        """Four missed probes and the previous pid set says nothing about now.
        Judging on it would flag a worker that was merely restarted.
        """
        worker = "cpu-processor@gap"
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json(worker, [11, 12]))):
            check_pool_has_live_processes(worker)

        stale = time.time() + celery_pool_healthcheck.SAMPLE_STALE_AFTER_S + 1
        monkeypatch.setattr(time, "time", lambda: stale)
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json(worker, [98, 99]))):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "storm check deferred" in reason

    def test_two_workers_do_not_share_a_sample(self):
        """One state file per node name — five prefork containers can each run this."""
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json("a@h", [11, 12]))):
            check_pool_has_live_processes("a@h")
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json("b@h", [98, 99]))):
            healthy, reason = check_pool_has_live_processes("b@h")

        assert healthy is True, "worker b's first probe must not be judged against worker a's"
        assert "storm check deferred" in reason

    def test_an_unwritable_sample_directory_degrades_instead_of_crashing(self, monkeypatch):
        """A crashing probe is itself a false unhealthy. Losing the storm check is
        the acceptable degradation; taking the container down is not.
        """
        monkeypatch.setattr(tempfile, "tempdir", "/proc/nonexistent-and-unwritable")
        with patch("subprocess.run", return_value=_completed(stdout=_stats_json("c@h", [11, 12]))):
            healthy, reason = check_pool_has_live_processes("c@h")

        assert healthy is True
        assert "2 live child process" in reason


@pytest.mark.unit
class TestNonPreforkPools:
    """A threads pool runs inside MainProcess, so the child checks do not apply."""

    THREADS_STATS_JSON = (
        '{"redaction@abc123": {"total": {}, "pool": {"implementation": '
        '"celery.concurrency.thread:TaskPool", "max-concurrency": 2, "threads": 0}}}'
    )

    def test_a_threads_pool_that_answers_is_healthy(self):
        """Live shape, captured from `celery-redaction`: no `processes` key at all.
        Requiring one would fail a perfectly healthy worker.
        """
        with patch("subprocess.run", return_value=_completed(stdout=self.THREADS_STATS_JSON)):
            healthy, reason = check_pool_has_live_processes("redaction@abc123")

        assert healthy is True
        assert "non-prefork" in reason

    def test_a_threads_pool_is_not_run_through_the_storm_check(self):
        """Its "pid set" is empty every probe; a storm verdict there would be noise."""
        for _ in range(3):
            with patch("subprocess.run", return_value=_completed(stdout=self.THREADS_STATS_JSON)):
                healthy, _ = check_pool_has_live_processes("redaction@abc123")
            assert healthy is True

    def test_an_uninterpretable_pool_section_fails_closed(self):
        """Reporting healthy on data it could not read is precisely the old bug."""
        with patch(
            "subprocess.run",
            return_value=_completed(stdout='{"w@h": {"pool": {"max-concurrency": 2}}}'),
        ):
            healthy, reason = check_pool_has_live_processes("w@h")

        assert healthy is False
        assert "no interpretable pool section" in reason


@pytest.mark.unit
class TestEveryCeleryWorkerUsesThePoolAwareCheck:
    """Issue #631 C5: three worker services are prefork-*capable* by env var and used
    to keep `inspect ping`, which cannot see a wedged forked pool. `core/celery.py`
    recommends flipping `GPU_WORKER_POOL=prefork`, so an operator taking that advice
    silently inherited the blind check. The script self-detects the pool type now, so
    every worker can use it — this stops a new worker being added without it.
    """

    COMPOSE_FILES = (
        "docker-compose.yml",
        "docker-compose.gpu-scale.yml",
        "docker-compose.blackwell.yml",
    )

    def _worker_services(self):
        root = _BACKEND_DIR.parent
        for filename in self.COMPOSE_FILES:
            path = root / filename
            compose = yaml.safe_load(path.read_text())
            for name, svc in (compose.get("services") or {}).items():
                command = " ".join(str(svc.get("command", "")).split())
                if "celery" in command and " worker " in f" {command} ":
                    yield filename, name, svc

    def test_at_least_one_worker_service_is_found(self):
        """Guard the guard: a matcher that selects nothing passes everything."""
        found = list(self._worker_services())
        assert len(found) >= 8, [f"{f}::{n}" for f, n, _ in found]

    def test_no_celery_worker_healthcheck_uses_the_blind_inspect_ping(self):
        offenders = [
            f"{filename}::{name}"
            for filename, name, svc in self._worker_services()
            if "inspect ping" in str((svc.get("healthcheck") or {}).get("test", ""))
        ]
        assert not offenders, (
            f"these worker healthchecks answer from MainProcess regardless of pool "
            f"health (issue #631): {offenders}"
        )

    def test_every_celery_worker_healthcheck_runs_the_pool_script(self):
        missing = [
            f"{filename}::{name}"
            for filename, name, svc in self._worker_services()
            if "celery_pool_healthcheck.py"
            not in str((svc.get("healthcheck") or {}).get("test", ""))
        ]
        assert not missing, f"worker services with no pool-aware healthcheck: {missing}"


@pytest.mark.unit
class TestProbeBudgetsMatchCompose:
    def test_the_subprocess_bound_sits_below_the_compose_healthcheck_timeout(self):
        """Otherwise Docker kills the probe first and the TimeoutExpired branch —
        the one that reports *why* the probe gave up — can never run. It could not,
        before this: the bound was 15s against a 10s compose timeout.
        """
        compose = yaml.safe_load((_BACKEND_DIR.parent / "docker-compose.yml").read_text())
        timeouts = {
            svc["healthcheck"]["timeout"]
            for svc in compose["services"].values()
            if "celery_pool_healthcheck" in str((svc.get("healthcheck") or {}).get("test", ""))
        }

        assert timeouts, "no service wires celery_pool_healthcheck.py — did it get renamed?"
        for timeout in timeouts:
            seconds = float(str(timeout).rstrip("s"))
            assert seconds > celery_pool_healthcheck.INSPECT_TIMEOUT_S, (
                f"probe bound {celery_pool_healthcheck.INSPECT_TIMEOUT_S}s is not below the "
                f"compose healthcheck timeout {timeout}"
            )

    def test_the_stale_window_spans_several_compose_probe_intervals(self):
        """The storm check needs the previous probe to still be comparable; a stale
        window shorter than the interval would defer on every single probe and
        detect nothing at all.
        """
        compose = yaml.safe_load((_BACKEND_DIR.parent / "docker-compose.yml").read_text())
        intervals = {
            svc["healthcheck"]["interval"]
            for svc in compose["services"].values()
            if "celery_pool_healthcheck" in str((svc.get("healthcheck") or {}).get("test", ""))
        }

        assert intervals
        for interval in intervals:
            seconds = float(str(interval).rstrip("s"))
            assert 2 * seconds <= celery_pool_healthcheck.SAMPLE_STALE_AFTER_S
