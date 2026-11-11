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

These tests replace the broker client (`_probe_app`) with a fake whose
`control.inspect(...).stats()` returns the exact reply shapes involved, including
the live-captured storm shape above; `_inspect_stats` and its deadline run for real.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import yaml
from kombu.exceptions import OperationalError

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from scripts import celery_pool_healthcheck
from scripts.celery_pool_healthcheck import check_pool_has_live_processes
from scripts.celery_pool_healthcheck import main

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


def _fake_app(stats_reply=None, *, stats_side_effect=None) -> MagicMock:
    """A stand-in for the bare Celery client; `stats()` answers like the real one."""
    app = MagicMock()
    stats = app.control.inspect.return_value.stats
    stats.return_value = stats_reply
    stats.side_effect = stats_side_effect
    return app


def _broker_replies(stats_json: str) -> AbstractContextManager[MagicMock]:
    """Make the broker answer `inspect stats` with *stats_json* (the `--json` shape)."""
    return patch.object(
        celery_pool_healthcheck, "_probe_app", return_value=_fake_app(json.loads(stats_json))
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
        with _broker_replies(HEALTHY_STATS_JSON):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is True
        assert "3 live child process" in reason

    def test_unhealthy_when_the_wedged_pool_shape_is_reported(self):
        """THE regression this script exists for: MainProcess answers, but
        with an empty child-process list — the exact live incident shape.
        """
        with _broker_replies(WEDGED_STATS_JSON):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "no live child processes" in reason

    def test_unhealthy_when_no_worker_replies(self):
        """Live-verified shape: `inspect(destination=[<nonexistent>]).stats()` returns
        None once the reply window closes (the CLI turned this into exit 69).
        """
        with patch.object(celery_pool_healthcheck, "_probe_app", return_value=_fake_app(None)):
            healthy, reason = check_pool_has_live_processes("cpu-processor@nonexistent")

        assert healthy is False
        assert "no worker replied" in reason

    def test_unhealthy_when_the_broker_is_unreachable(self):
        """Live-verified: a refused broker surfaces as kombu's OperationalError."""
        app = _fake_app(stats_side_effect=OperationalError("Error 111 connecting. Refused."))
        with patch.object(celery_pool_healthcheck, "_probe_app", return_value=app):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "OperationalError" in reason
        assert "Refused" in reason

    def test_unhealthy_when_payload_is_an_empty_object(self):
        with _broker_replies("{}"):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False
        assert "no worker replied" in reason

    def test_unhealthy_when_the_broker_exchange_outlives_the_deadline(self, monkeypatch):
        """kombu publishes control messages with `retry=True`, so a blackholed broker
        blocks rather than failing. The deadline must interrupt it and say so, before
        Docker's own timeout SIGKILLs the probe with no reason recorded.
        """
        monkeypatch.setattr(celery_pool_healthcheck, "INSPECT_TIMEOUT_S", 0.2)
        app = _fake_app(stats_side_effect=lambda: time.sleep(5))
        started = time.monotonic()
        with patch.object(celery_pool_healthcheck, "_probe_app", return_value=app):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert time.monotonic() - started < 2, "the deadline did not interrupt the wait"
        assert healthy is False
        assert "no answer within" in reason
        app.close.assert_called_once()

    def test_pool_key_missing_entirely_is_unhealthy_not_a_crash(self):
        """A future celery version could drop or rename `pool` — this must
        degrade to unhealthy, never raise (a crashing healthcheck is itself a
        false-unhealthy that could flap a fine worker, but MUST NOT silently
        report healthy on data it cannot interpret).
        """
        with _broker_replies('{"cpu-processor@abc123": {}}'):
            healthy, reason = check_pool_has_live_processes("cpu-processor@abc123")

        assert healthy is False


@pytest.mark.unit
class TestMainExitCode:
    def test_exit_zero_when_healthy(self):
        with _broker_replies(HEALTHY_STATS_JSON):
            assert main(["celery_pool_healthcheck.py", "cpu-processor@abc123"]) == 0

    def test_exit_one_when_unhealthy(self):
        with _broker_replies(WEDGED_STATS_JSON):
            assert main(["celery_pool_healthcheck.py", "cpu-processor@abc123"]) == 1

    def test_exit_two_on_bad_usage(self):
        assert main(["celery_pool_healthcheck.py"]) == 2

    def test_asks_only_the_named_node_through_the_documented_inspect_api(self):
        """Pin the request: `app.control.inspect(destination=[node]).stats()`. Without
        the destination every worker on the broker answers and the first reply, not
        necessarily this container's, would be judged.
        """
        app = _fake_app(json.loads(HEALTHY_STATS_JSON))
        with patch.object(celery_pool_healthcheck, "_probe_app", return_value=app):
            healthy, reason = check_pool_has_live_processes("cpu-processor@myhost")

        assert app.control.inspect.call_args.kwargs["destination"] == ["cpu-processor@myhost"]
        app.control.inspect.return_value.stats.assert_called_once_with()
        app.close.assert_called_once()
        assert healthy is True, "the reply to that request is what the verdict is built from"
        assert "3 live child process" in reason

    def test_asks_for_a_reply_window_wider_than_celerys_one_second_default(self):
        """`inspect stats` is answered by the MainProcess event loop; celery's own
        1.0s default (`Inspect(timeout=1.0)`) misses it on a busy worker and flips
        the container unhealthy for a reason unrelated to pool health.
        """
        app = _fake_app(json.loads(HEALTHY_STATS_JSON))
        with patch.object(celery_pool_healthcheck, "_probe_app", return_value=app):
            check_pool_has_live_processes("cpu-processor@myhost")

        reply_window = app.control.inspect.call_args.kwargs["timeout"]
        assert reply_window > 1.0
        assert reply_window < celery_pool_healthcheck.INSPECT_TIMEOUT_S


@pytest.mark.unit
class TestTheProbeStaysCheap:
    """It runs every 30s in every worker container on the host. Shelling out to
    `celery -A app.core.celery inspect stats` cost ~2.4s of CPU per probe (measured
    in a live cpu-processor container): the click CLI with its flower / click-repl /
    prompt_toolkit plugins, then the app and pydantic-settings, for a broker round
    trip under 0.1s.
    """

    _IMPORTED = (
        "import sys;"
        "from scripts.celery_pool_healthcheck import _probe_app;"
        "app = _probe_app();"
        "heavy = ('app.core.celery', 'app.core.config', 'pydantic_settings', 'torch',"
        " 'celery.bin.celery', 'flower', 'prompt_toolkit');"
        "print(sorted(m for m in heavy if m in sys.modules) or 'none')"
    )

    def test_building_the_client_imports_neither_the_app_nor_the_cli(self, run_in_clean_process):
        assert run_in_clean_process(self._IMPORTED, SKIP_CELERY="false") == "none"

    def test_no_subprocess_is_spawned_for_the_probe(self):
        app = _fake_app(json.loads(HEALTHY_STATS_JSON))
        with (
            patch.object(celery_pool_healthcheck, "_probe_app", return_value=app),
            patch("subprocess.run") as run,
            patch("subprocess.Popen") as popen,
        ):
            healthy, _ = check_pool_has_live_processes("cpu-processor@myhost")

        assert healthy is True
        run.assert_not_called()
        popen.assert_not_called()


@pytest.mark.unit
class TestProbeClientMatchesTheWorkersBroker:
    """The bare client must reach the same broker, over the same TLS, as the app."""

    _PROBE = (
        "from scripts.celery_pool_healthcheck import _probe_app;"
        "app = _probe_app();"
        "print(app.conf.broker_url, app.conf.broker_use_ssl, sep='|')"
    )
    _APP = (
        "from app.core.celery import celery_app;"
        "print(celery_app.conf.broker_url, celery_app.conf.broker_use_ssl, sep='|')"
    )
    _UNSET = ("CELERY_BROKER_URL", "REDIS_URL", "REDIS_PASSWORD", "REDIS_USE_TLS")

    @pytest.mark.parametrize(
        "env",
        [
            {"REDIS_HOST": "redis", "REDIS_PORT": "6379"},
            {"REDIS_HOST": "cache.internal", "REDIS_PORT": "6380", "REDIS_PASSWORD": "pw"},
            {"REDIS_HOST": "redis", "REDIS_PORT": "6379", "REDIS_USE_TLS": "true"},
            {
                "REDIS_HOST": "redis",
                "REDIS_USE_TLS": "true",
                "CELERY_REDIS_SSL_CERT_REQS": "none",
            },
            {"REDIS_HOST": "ignored", "REDIS_URL": "redis://from-redis-url:1/0"},
            {"REDIS_URL": "redis://a:1/0", "CELERY_BROKER_URL": "rediss://explicit:2/0"},
            {"REDIS_HOST": "redis", "REDIS_URL": "", "CELERY_BROKER_URL": ""},
        ],
        ids=["plain", "password", "tls", "tls-no-verify", "redis-url", "broker-url", "empty-urls"],
    )
    def test_same_broker_url_and_tls_options_as_the_app(self, run_in_clean_process, env):
        probe = run_in_clean_process(self._PROBE, unset=self._UNSET, **env)
        app = run_in_clean_process(self._APP, unset=self._UNSET, **env)

        assert probe == app
        assert probe.split("|")[0], "an empty broker URL would fall back to amqp://"


@pytest.mark.unit
class TestRespawnStormDetection:
    """The failure `pool.processes`-non-empty cannot see (issue #631)."""

    def test_a_churning_pool_with_no_task_progress_is_unhealthy(self):
        """THE incident shape, live-captured: every pid replaced between probes,
        `total` frozen. The pre-fix script exits 0 on both of these payloads.
        """
        worker = "cpu-processor@storm"
        with _broker_replies(_stats_json(worker, [11, 12])):
            first_healthy, _ = check_pool_has_live_processes(worker)
        with _broker_replies(_stats_json(worker, [98, 99])):
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
        with _broker_replies(_stats_json(worker, [11, 12], {"work": 40})):
            check_pool_has_live_processes(worker)
        with _broker_replies(_stats_json(worker, [98, 99], {"work": 81})):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "accepting tasks" in reason

    def test_a_pool_that_keeps_any_child_across_probes_is_healthy(self):
        worker = "cpu-processor@steady"
        with _broker_replies(_stats_json(worker, [11, 12, 13])):
            check_pool_has_live_processes(worker)
        with _broker_replies(_stats_json(worker, [11, 12, 99])):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "carried over" in reason

    def test_the_first_probe_after_a_restart_defers_rather_than_guesses(self):
        worker = "cpu-processor@fresh"
        with _broker_replies(_stats_json(worker, [11, 12])):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "storm check deferred" in reason

    def test_a_stale_prior_sample_is_not_compared_against(self, monkeypatch):
        """Four missed probes and the previous pid set says nothing about now.
        Judging on it would flag a worker that was merely restarted.
        """
        worker = "cpu-processor@gap"
        with _broker_replies(_stats_json(worker, [11, 12])):
            check_pool_has_live_processes(worker)

        stale = time.time() + celery_pool_healthcheck.SAMPLE_STALE_AFTER_S + 1
        monkeypatch.setattr(time, "time", lambda: stale)
        with _broker_replies(_stats_json(worker, [98, 99])):
            healthy, reason = check_pool_has_live_processes(worker)

        assert healthy is True
        assert "storm check deferred" in reason

    def test_two_workers_do_not_share_a_sample(self):
        """One state file per node name — five prefork containers can each run this."""
        with _broker_replies(_stats_json("a@h", [11, 12])):
            check_pool_has_live_processes("a@h")
        with _broker_replies(_stats_json("b@h", [98, 99])):
            healthy, reason = check_pool_has_live_processes("b@h")

        assert healthy is True, "worker b's first probe must not be judged against worker a's"
        assert "storm check deferred" in reason

    def test_an_unwritable_sample_directory_degrades_instead_of_crashing(self, monkeypatch):
        """A crashing probe is itself a false unhealthy. Losing the storm check is
        the acceptable degradation; taking the container down is not.
        """
        monkeypatch.setattr(tempfile, "tempdir", "/proc/nonexistent-and-unwritable")
        with _broker_replies(_stats_json("c@h", [11, 12])):
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
        with _broker_replies(self.THREADS_STATS_JSON):
            healthy, reason = check_pool_has_live_processes("redaction@abc123")

        assert healthy is True
        assert "non-prefork" in reason

    def test_a_threads_pool_is_not_run_through_the_storm_check(self):
        """Its "pid set" is empty every probe; a storm verdict there would be noise."""
        for _ in range(3):
            with _broker_replies(self.THREADS_STATS_JSON):
                healthy, _ = check_pool_has_live_processes("redaction@abc123")
            assert healthy is True

    def test_an_uninterpretable_pool_section_fails_closed(self):
        """Reporting healthy on data it could not read is precisely the old bug."""
        with _broker_replies('{"w@h": {"pool": {"max-concurrency": 2}}}'):
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
    def test_the_probe_deadline_sits_below_the_compose_healthcheck_timeout(self):
        """Otherwise Docker kills the probe first and the deadline branch — the one
        that reports *why* the probe gave up — can never run. It could not, once
        before: the bound was 15s against a 10s compose timeout.
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
