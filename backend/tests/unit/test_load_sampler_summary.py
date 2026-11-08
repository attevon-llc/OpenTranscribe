"""The load test's stack sampler and its run summary (``scripts/perf/load/``).

The sampler's parsers are fed the exact text each tool prints; its guards (a ``--fresh`` stack
only, never GPU 0) are checked to refuse. The summary is run on a synthetic run directory and
must fail it when a limit in ``thresholds.toml`` is broken.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

from app.core.queues import CeleryQueues
from tests.fixtures.locust_stub import LOAD_DIR


def _module(monkeypatch, name: str):
    monkeypatch.syspath_prepend(str(LOAD_DIR))
    for dep in ("scenario", name):
        spec = importlib.util.spec_from_file_location(dep, LOAD_DIR / f"{dep}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, dep, module)
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.fixture
def sampler(monkeypatch):
    return _module(monkeypatch, "sampler")


@pytest.fixture
def summarize(monkeypatch):
    return _module(monkeypatch, "summarize")


# --- sampler ---------------------------------------------------------------------------------


def test_the_sampler_reads_every_celery_queue(sampler):
    assert tuple(CeleryQueues.ALL) == sampler.QUEUES


@pytest.mark.parametrize("raw", ["0", "0,1", "1,0,2", ""])
def test_gpu_0_and_an_empty_list_are_refused(sampler, raw):
    with pytest.raises(ValueError):
        sampler.parse_gpus(raw)


def test_the_project_gpus_are_accepted(sampler):
    assert sampler.parse_gpus("1,2") == (1, 2)


def test_the_sampler_refuses_a_stack_that_is_not_fresh(sampler, capsys):
    assert sampler.main(["--out", "/nonexistent-run-dir"]) == 2
    assert "--fresh NAME is required" in capsys.readouterr().err


def test_only_the_requested_gpus_are_recorded(sampler):
    text = "0, 40000, 49140, 99\n1, 3000, 12288, 45\n2, 100, 49140, 0\n"

    rows = sampler.parse_nvidia(text, (1,))

    assert rows == [
        ("gpu", "gpu1:mem_used_mib", 3000.0),
        ("gpu", "gpu1:mem_total_mib", 12288.0),
        ("gpu", "gpu1:util_pct", 45.0),
    ]


def test_pg_states_and_long_idle_transactions_are_counted(sampler):
    text = "active,3,0,0.5\nidle,20,0,0\nidle in transaction,2,1,12.5\n"

    rows = dict(((s, n), v) for s, n, v in sampler.parse_pg(text))

    assert rows[("pg", "state:active")] == 3
    assert rows[("pg", "state:idle in transaction")] == 2
    assert rows[("pg", "idle_in_transaction_over_1s")] == 1
    assert rows[("pg", "oldest_xact_s")] == 12.5


def test_pool_gauges_are_read_from_the_metrics_text(sampler):
    text = (
        "# HELP db_pool_capacity x\n# TYPE db_pool_capacity gauge\n"
        "db_pool_capacity 30.0\ndb_pool_checked_out 7.0\n"
        'http_requests_in_flight{handler="all"} 4.0\nother_metric 1\n'
    )

    rows = sampler.parse_metrics(text)

    assert rows == [
        ("pool", "db_pool_capacity", 30.0),
        ("pool", "db_pool_checked_out", 7.0),
        ("pool", 'http_requests_in_flight{handler="all"}', 4.0),
    ]


def test_queue_depths_pair_with_the_queue_names(sampler):
    text = "\n".join(str(i) for i in range(len(sampler.QUEUES))) + "\n"

    rows = sampler.parse_queues(text)

    assert rows[0] == ("queue", "gpu", 0.0)
    assert rows[-1] == ("queue", "celery", float(len(sampler.QUEUES) - 1))
    with pytest.raises(ValueError):
        sampler.parse_queues("1\n2\n")


def test_container_stats_are_named_by_compose_service(sampler):
    text = (
        '{"ID":"abc123def456","Name":"otfresh-p-backend-1","CPUPerc":"150.25%",'
        '"MemUsage":"1.5GiB / 31GiB"}\n'
    )

    rows = sampler.parse_docker_stats(text, {"abc123def456": "backend"})

    assert rows == [
        ("container", "cpu_pct:backend", 150.25),
        ("container", "mem_bytes:backend", 1.5 * 1024**3),
    ]


# --- summary ---------------------------------------------------------------------------------

_STATS_HEADER = ["Type", "Name", "Request Count", "Failure Count", "50%", "95%", "99%"]


def _write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _run_dir(
    tmp_path: Path,
    *,
    read_p95: int,
    failures: int,
    idle_txn: int = 0,
    sweeps: int = 2,
    pool_sweeps: int | None = None,
    api_connections: int = 5,
) -> Path:
    """``sweeps`` sampler sweeps; only the first ``pool_sweeps`` got an API answer (default all)."""
    pool_sweeps = sweeps if pool_sweeps is None else pool_sweeps
    samples: list[list[object]] = []
    for i in range(sweeps):
        ts = 1.0 + 5 * i
        samples += [
            [ts, "pg", "idle_in_transaction_over_1s", idle_txn if i == 1 else 0],
            [ts, "pgapi", "connections", api_connections],
            [ts, "pgapi", "capacity", 60],
            [ts, "container", "mem_bytes:backend", 1000 + 100 * (i == sweeps - 1)],
        ]
        if i < pool_sweeps:
            samples += [
                [ts, "pool", "db_pool_capacity", 30],
                [ts, "pool", "db_pool_checked_out", 5],
            ]
    _write(
        tmp_path / "locust_stats.csv",
        _STATS_HEADER,
        [
            ["GET", "files.list", 1000, failures, 80, read_p95, read_p95 + 100],
            ["POST", "comments.create", 100, 0, 50, 300, 600],
            ["SSE", "mock-gpt first token", 50, 0, 900, 1500, 2000],
            ["WS", "echo round trip", 500, 0, 5, 40, 90],
            ["GET", "rarely called", 3, 0, 9000, 9000, 9000],
            ["", "Aggregated", 1653, failures, 80, 400, 900],
        ],
    )
    _write(
        tmp_path / "locust_failures.csv",
        ["Method", "Name", "Error", "Occurrences"],
        [["GET", "files.list", "HTTP 500", failures]] if failures else [],
    )
    _write(tmp_path / "samples.csv", ["ts", "source", "name", "value"], samples)
    return tmp_path


def _limits() -> dict:
    return tomllib.loads((LOAD_DIR / "thresholds.toml").read_text())


def test_a_run_inside_every_limit_passes(summarize, tmp_path):
    report, verdict = summarize.summarize(_run_dir(tmp_path, read_p95=400, failures=0), _limits())

    assert verdict.failures == []
    assert "**Verdict: PASS**" in report
    assert "| too few |" in report  # the 3-request row is shown but not judged


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"read_p95": 1500, "failures": 0}, "GET files.list: p95 1500.0"),
        ({"read_p95": 400, "failures": 50}, "error rate"),
        ({"read_p95": 400, "failures": 0, "idle_txn": 2}, "idle in a transaction"),
    ],
)
def test_a_broken_limit_fails_the_run(summarize, tmp_path, kwargs, reason):
    report, verdict = summarize.summarize(_run_dir(tmp_path, **kwargs), _limits())

    assert "**Verdict: FAIL**" in report
    assert any(reason in failure for failure in verdict.failures), verdict.failures


def test_the_command_writes_the_report_and_exits_1_on_a_failed_run(summarize, tmp_path):
    run_dir = _run_dir(tmp_path, read_p95=5000, failures=0)

    assert summarize.main([str(run_dir)]) == 1
    assert (run_dir / "summary.md").read_text().startswith("# Load test:")


def test_a_run_without_locust_output_is_unusable(summarize, tmp_path):
    assert summarize.main([str(tmp_path)]) == 2


# --- T7: a run that sampled almost nothing, or whose pool was full, cannot pass --------------


def test_a_saturated_pool_fails_the_run_even_when_the_api_stopped_answering_metrics(
    summarize, tmp_path
):
    # M2 run 05: 2 of 111 /metrics samples arrived, both unsaturated, and the report said
    # "0.0% saturated". Postgres is asked directly, so every sweep has the API's connections.
    run_dir = _run_dir(
        tmp_path, read_p95=400, failures=0, sweeps=111, pool_sweeps=2, api_connections=60
    )

    report, verdict = summarize.summarize(run_dir, _limits())

    assert "**Verdict: FAIL**" in report
    assert any("held its whole DB pool" in f for f in verdict.failures), verdict.failures


def test_a_run_whose_pg_stat_activity_samples_are_mostly_missing_fails(summarize, tmp_path):
    run_dir = _run_dir(tmp_path, read_p95=400, failures=0, sweeps=10)
    rows: list[list[object]] = [list(r) for r in csv.reader((run_dir / "samples.csv").open())]
    kept = [r for r in rows[1:] if r[1] != "pgapi" or r[0] in ("1.0", "6.0")]
    _write(run_dir / "samples.csv", [str(h) for h in rows[0]], kept)

    _report, verdict = summarize.summarize(run_dir, _limits())

    assert any("coverage for 'pgapi' is 20%" in f for f in verdict.failures), verdict.failures


def test_a_full_set_of_unsaturated_samples_passes_the_coverage_rule(summarize, tmp_path):
    run_dir = _run_dir(tmp_path, read_p95=400, failures=0, sweeps=20, pool_sweeps=4)

    _report, verdict = summarize.summarize(run_dir, _limits())

    assert verdict.failures == []


def test_the_api_connection_query_names_the_container_and_the_app_name(sampler):
    sql = sampler.API_CONN_SQL.format(app=sampler.API_APP_NAME, ips="172.18.0.5")

    assert "application_name = 'opentranscribe-api'" in sql
    assert "'172.18.0.5'" in sql
    assert sampler.parse_api_connections("37\n", 60) == [
        ("pgapi", "connections", 37.0),
        ("pgapi", "capacity", 60.0),
    ]
    assert sampler.parse_ips("172.18.0.5 \n") == ["172.18.0.5"]
    assert sampler.parse_ips("x'; DROP") == []  # nothing but an address reaches the SQL


@pytest.mark.parametrize(("env", "want"), [("20 40", 60.0), ("5 0", 5.0), (" ", 60.0)])
def test_the_pool_capacity_comes_from_the_containers_env(sampler, env, want):
    assert sampler.parse_pool_capacity(env) == want
