#!/usr/bin/env python3
"""Sample a ``--fresh`` stack every few seconds while a load test runs.

    scripts/perf/load/sampler.py --fresh perf1337 --out RUN_DIR [--interval 5] [--gpus 1,2]

Appends one row per value to ``RUN_DIR/samples.csv`` (``ts,source,name,value``) until it is
sent SIGTERM or SIGINT (``run.sh`` does that when Locust exits), or ``--duration`` passes:

- ``pg``: ``pg_stat_activity`` connections by state, connections idle in a transaction for
  more than a second, and the oldest open transaction's age;
- ``pgapi``: how many ``pg_stat_activity`` connections the API holds (by the backend container's
  IP, or ``application_name='opentranscribe-api'``) and the pool capacity they are judged
  against. Asked of Postgres, so it still answers when the API is saturated;
- ``pool``: the API's ``db_pool_*`` gauges and ``http_requests_in_flight`` from ``/metrics``;
- ``queue``: each Celery queue's depth in Redis (all priority levels summed);
- ``container``: CPU percent and memory bytes of every container of this stack only, found by
  its compose project label (``otfresh-NAME``), never by a name grep;
- ``gpu``: memory used and utilisation of the ``--gpus`` cards only. GPU 0 is refused: it runs
  unrelated work on this host.

Standard library only, and it never reads a secret: the Postgres user and the Redis password
are expanded inside their own containers.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import scenario

#: ``CeleryQueues.ALL`` (backend/app/core/queues.py); a unit test keeps the two equal.
QUEUES = (
    'gpu',
    'download',
    'cpu',
    'nlp',
    'embedding',
    'utility',
    'cloud-asr',
    'cpu-transcribe',
    'gpu-transcribe',
    'gpu-diarize',
    'redaction',
    'celery',
)
FORBIDDEN_GPUS = frozenset({0})
METRIC_PREFIXES = ('db_pool_', 'http_requests_in_flight')
COMMAND_TIMEOUT_S = 4.0

PG_SQL = (
    "SELECT coalesce(state, 'none'), count(*), "
    "count(*) FILTER (WHERE state LIKE 'idle in transaction%' "
    "AND now() - xact_start > interval '1 second'), "
    'coalesce(max(extract(epoch FROM now() - xact_start)), 0) '
    'FROM pg_stat_activity WHERE datname = current_database() GROUP BY 1'
)
API_APP_NAME = 'opentranscribe-api'
API_CONN_SQL = (
    'SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() '
    "AND (application_name = '{app}' OR client_addr = ANY (string_to_array('{ips}', ',')::inet[]))"
)
DEFAULT_POOL_CAPACITY = 20 + 40  # DB_POOL_SIZE + DB_MAX_OVERFLOW (core/settings_capacity.py)
_IP_RE = re.compile(r'^[0-9a-fA-F:.]+$')
PG_SHELL = 'psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-opentranscribe}" -AtF, -c "$0"'
# kombu's Redis transport keeps a priority queue as NAME plus NAME\x06\x16<1..9>.
QUEUE_LUA = (
    'local out = {} for _, q in ipairs(ARGV) do local n = redis.call("LLEN", q) '
    'for p = 1, 9 do n = n + redis.call("LLEN", q .. "\\6\\22" .. p) end '
    'table.insert(out, n) end return out'
)
REDIS_SHELL = 'redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"} --no-auth-warning EVAL "$0" 0 "$@"'
NVIDIA_QUERY = '--query-gpu=index,memory.used,memory.total,utilization.gpu'

Row = tuple[str, str, float]  # source, name, value
_UNITS = {'b': 1, 'kib': 1024, 'mib': 1024**2, 'gib': 1024**3, 'kb': 1e3, 'mb': 1e6, 'gb': 1e9}


def parse_gpus(raw: str) -> tuple[int, ...]:
    """``"1,2"`` -> ``(1, 2)``; refuses GPU 0 and anything that is not a card index."""
    gpus = tuple(int(part) for part in raw.split(',') if part.strip())
    if not gpus:
        raise ValueError('--gpus needs at least one card index')
    if FORBIDDEN_GPUS & set(gpus):
        raise ValueError('GPU 0 runs unrelated work on this host and is never sampled')
    return gpus


def parse_pg(text: str) -> list[Row]:
    rows: list[Row] = []
    long_idle = oldest = 0.0
    for line in text.splitlines():
        parts = line.split(',')
        if len(parts) != 4:
            continue
        state, count, idle, age = parts
        rows.append(('pg', f'state:{state}', float(count)))
        long_idle += float(idle)
        oldest = max(oldest, float(age))
    rows += [('pg', 'idle_in_transaction_over_1s', long_idle), ('pg', 'oldest_xact_s', oldest)]
    return rows


def parse_api_connections(text: str, capacity: float) -> list[Row]:
    """``count(*)`` of the API's connections, paired with the capacity they are judged against."""
    return [
        ('pgapi', 'connections', float(text.strip())),
        ('pgapi', 'capacity', float(capacity)),
    ]


def parse_ips(text: str) -> list[str]:
    """Container IPs from ``docker inspect --format '{{range ...}}{{.IPAddress}} {{end}}'``."""
    return [ip for ip in text.split() if _IP_RE.match(ip)]


def parse_pool_capacity(text: str) -> float:
    """``DB_POOL_SIZE DB_MAX_OVERFLOW`` as printed by the container; unset values use the default."""
    size, overflow = (text.split() + ['', ''])[:2]
    try:
        return float(int(size) + int(overflow))
    except ValueError:
        return float(DEFAULT_POOL_CAPACITY)


def parse_metrics(text: str) -> list[Row]:
    """The pool and in-flight gauges of a Prometheus text exposition."""
    rows: list[Row] = []
    for line in text.splitlines():
        if line.startswith(METRIC_PREFIXES):
            name, _, value = line.rpartition(' ')
            try:
                rows.append(('pool', name, float(value)))
            except ValueError:
                continue
    return rows


def parse_queues(text: str) -> list[Row]:
    values = [line.strip() for line in text.splitlines() if line.strip()]
    if len(values) != len(QUEUES):
        raise ValueError(f'expected {len(QUEUES)} queue depths, got {values!r}')
    return [('queue', name, float(v)) for name, v in zip(QUEUES, values, strict=True)]


def _bytes(size: str) -> float:
    match = re.fullmatch(r'\s*([\d.]+)\s*([A-Za-z]+)\s*', size)
    if not match:
        raise ValueError(f'unreadable size {size!r}')
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def parse_docker_stats(text: str, services: dict[str, str]) -> list[Row]:
    """CPU percent and memory bytes per compose service from ``docker stats`` JSON lines."""
    rows: list[Row] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        stat = json.loads(line)
        service = next(
            (s for cid, s in services.items() if str(stat.get('ID', '')).startswith(cid[:12])),
            stat.get('Name', '?'),
        )
        rows.append(('container', f'cpu_pct:{service}', float(stat['CPUPerc'].rstrip('%'))))
        rows.append(('container', f'mem_bytes:{service}', _bytes(stat['MemUsage'].split('/')[0])))
    return rows


def parse_nvidia(text: str, gpus: tuple[int, ...]) -> list[Row]:
    rows: list[Row] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) != 4 or not parts[0].isdigit():
            continue
        index = int(parts[0])
        if index not in gpus:  # never record a card this run was not given
            continue
        rows.append(('gpu', f'gpu{index}:mem_used_mib', float(parts[1])))
        rows.append(('gpu', f'gpu{index}:mem_total_mib', float(parts[2])))
        rows.append(('gpu', f'gpu{index}:util_pct', float(parts[3])))
    return rows


def _run(argv: list[str]) -> str:
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=True
    )
    return proc.stdout


def project_containers(project: str) -> dict[str, str]:
    """``container id -> compose service`` for the stack's containers (by project label)."""
    out = _run(
        [
            'docker',
            'ps',
            '--filter',
            f'label=com.docker.compose.project={project}',
            '--format',
            '{{.ID}}\t{{.Label "com.docker.compose.service"}}',
        ]
    )
    return dict(line.split('\t', 1) for line in out.splitlines() if '\t' in line)


class Sampler:
    """One sweep of every source; a source that fails records an ``error`` row and is skipped."""

    def __init__(self, base_url: str, containers: dict[str, str], gpus: tuple[int, ...]) -> None:
        self.base_url = base_url
        self.containers = containers
        self.gpus = gpus
        by_service = {service: cid for cid, service in containers.items()}
        self.pg = by_service.get('postgres')
        self.redis = by_service.get('redis')
        self.backend = by_service.get('backend')
        self._api_ips: str | None = None
        self._capacity: float | None = None

    def _api_identity(self) -> tuple[str, float]:
        """The backend's IPs and pool capacity, read once (the stack does not change mid-run)."""
        if self._api_ips is None or self._capacity is None:
            backend = self.backend or ''
            ips = _run(
                [
                    'docker',
                    'inspect',
                    '--format',
                    '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}',
                    backend,
                ]
            )
            env = _run(
                ['docker', 'exec', backend, 'sh', '-c', 'echo "$DB_POOL_SIZE $DB_MAX_OVERFLOW"']
            )
            self._api_ips = ','.join(parse_ips(ips))
            self._capacity = parse_pool_capacity(env)
        return self._api_ips, self._capacity

    def sources(self) -> dict[str, Callable[[], list[Row]]]:
        out: dict[str, Callable[[], list[Row]]] = {
            'pool': lambda: parse_metrics(self._metrics()),
            'container': lambda: parse_docker_stats(
                _run(
                    ['docker', 'stats', '--no-stream', '--format', '{{json .}}', *self.containers]
                ),
                self.containers,
            ),
            'gpu': lambda: parse_nvidia(
                _run(
                    [
                        'nvidia-smi',
                        NVIDIA_QUERY,
                        '--format=csv,noheader,nounits',
                        '-i',
                        ','.join(map(str, self.gpus)),
                    ]
                ),
                self.gpus,
            ),
        }
        if self.pg:
            pg = self.pg
            out['pg'] = lambda: parse_pg(_run(['docker', 'exec', pg, 'sh', '-c', PG_SHELL, PG_SQL]))
        if self.pg and self.backend:
            out['pgapi'] = self._api_connections
        if self.redis:
            redis = self.redis
            out['queue'] = lambda: parse_queues(
                _run(['docker', 'exec', redis, 'sh', '-c', REDIS_SHELL, QUEUE_LUA, *QUEUES])
            )
        return out

    def _api_connections(self) -> list[Row]:
        ips, capacity = self._api_identity()
        sql = API_CONN_SQL.format(app=API_APP_NAME, ips=ips)
        out = _run(['docker', 'exec', str(self.pg), 'sh', '-c', PG_SHELL, sql])
        return parse_api_connections(out, capacity)

    def _metrics(self) -> str:
        with urllib.request.urlopen(f'{self.base_url}/metrics', timeout=COMMAND_TIMEOUT_S) as resp:
            body: bytes = resp.read()
        return body.decode()

    def sweep(self) -> list[Row]:
        rows: list[Row] = []
        for name, source in self.sources().items():
            try:
                rows += source()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                rows.append(('error', f'{name}:{type(exc).__name__}', 1.0))
        return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--fresh', default='', help='the ./opentr.sh --fresh deployment')
    parser.add_argument('--out', required=True, type=Path, help='the run directory')
    parser.add_argument('--interval', type=float, default=5.0, help='seconds between samples')
    parser.add_argument('--gpus', default='1', help='card indexes to sample, e.g. 1,2 (never 0)')
    parser.add_argument('--duration', type=float, default=0.0, help='stop after N s (0: on signal)')
    args = parser.parse_args(argv)
    try:
        target = scenario.resolve_target(args.fresh)
        gpus = parse_gpus(args.gpus)
        containers = project_containers(target.compose_project)
    except (scenario.NotIsolatedError, ValueError) as exc:
        print(f'sampler: {exc}', file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'sampler: cannot list the stack containers: {exc}', file=sys.stderr)
        return 2
    if not containers:
        print(f'sampler: no containers in project {target.compose_project}', file=sys.stderr)
        return 2

    stop = False

    def _stop(*_args: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sampler = Sampler(target.base_url, containers, gpus)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / 'samples.csv'
    started = time.monotonic()
    with path.open('a', newline='') as fh:
        writer = csv.writer(fh)
        if fh.tell() == 0:
            writer.writerow(('ts', 'source', 'name', 'value'))
        while not stop and not (args.duration and time.monotonic() - started >= args.duration):
            tick = time.monotonic()
            now = time.time()
            writer.writerows((f'{now:.1f}', *row) for row in sampler.sweep())
            fh.flush()
            while not stop and time.monotonic() - tick < args.interval:
                time.sleep(0.2)
    return 0


if __name__ == '__main__':
    sys.exit(main())
