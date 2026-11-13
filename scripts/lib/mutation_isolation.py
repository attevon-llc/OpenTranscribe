#!/usr/bin/env python3
"""Per-module isolation for `run-mutation-tests.sh --parallel` on ONE --fresh stack.

One Postgres server holds a database per module (``ot_mut_<key>``, copied from a template that
the repo's own migrate step brought to head); one Redis server holds a DB index per module.
Both are selected by env (``POSTGRES_DB``, ``REDIS_URL``), which the app already honours.

Why this isolates (docs, read for the pinned versions):
  * PostgreSQL 17 advisory locks are scoped to the current database (``pg_locks.database`` holds
    the database OID): https://www.postgresql.org/docs/17/view-pg-locks.html and
    https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS
  * CREATE DATABASE ... TEMPLATE needs the template idle:
    https://www.postgresql.org/docs/17/manage-ag-templatedbs.html ;
    DROP DATABASE ... WITH (FORCE): https://www.postgresql.org/docs/17/sql-dropdatabase.html
  * Redis SELECT/FLUSHDB act on ONE index: https://redis.io/docs/latest/commands/select/ ,
    https://redis.io/docs/latest/commands/flushdb/ ; redis-py takes the index from the URL path:
    https://redis.readthedocs.io/en/stable/connections.html

Subcommands (stdout is machine-readable, never carries the Redis password):
  plan --modules a,b [--redis-databases N]   JSON {key: {db, redis_index}}
  provision --modules a,b                    template + per-module DBs + FLUSHDB (otfresh-* only)
  probe --modules a,b                        isolation probe with positive controls
  budget --jobs P --children N               Postgres connection preflight
  env --module k --index I                   `export` lines for one child (secret: eval, don't print)
  compare LOG_A LOG_B                        mutant-by-mutant verdict diff
  judge --module k --log L --baseline B      ratchet-direction report for a parallel run
Needs the target stack in the environment (st_target exports it) like the other stack libs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import NamedTuple

from stack_core import (
    MIGRATE_ALLOWED_PREFIXES,
    WRONG_PROJECT,
    StackError,
    container_exec,
    resolve,
)

DB_PREFIX = 'ot_mut_'
TEMPLATE_DB = DB_PREFIX + 'template'
DB_NAME_RE = re.compile(r'^ot_mut_[a-z_]+$')
# Index 0 is the stack's broker/results, 1 the cache (redis_cache_service.py), so never touched.
RESERVED_REDIS_INDEXES = frozenset({0, 1})
FIRST_MODULE_INDEX = 2
PROBE_LOCK_KEY = (990389, 2)
CONN_PER_CHILD = 3
EXIT_MISCONFIGURED = 3
EXIT_NOT_MEASURED = 4
SUMMARY_RE = re.compile(r'^=== OT-MUTATION-SUMMARY (BEGIN|END) key=(\S+)')


class Slot(NamedTuple):
    db: str
    redis_index: int


class IsolationError(Exception):
    def __init__(self, message: str, code: int = EXIT_MISCONFIGURED):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- plan (pure)


def db_name(key: str) -> str:
    name = DB_PREFIX + key
    if not DB_NAME_RE.match(name):
        raise IsolationError(f'module key {key!r} does not map to a legal database name')
    return name


def plan(modules: list[str], redis_databases: int = 16) -> dict[str, Slot]:
    keys = sorted(set(modules))
    if len(keys) != len(modules):
        raise IsolationError('duplicate module in the plan')
    out: dict[str, Slot] = {}
    for i, key in enumerate(keys):
        idx = FIRST_MODULE_INDEX + i
        if idx >= redis_databases:
            raise IsolationError(
                f'{len(keys)} modules need Redis index {idx} but the server has only '
                f'{redis_databases} databases (indexes 0 and 1 are reserved); run fewer modules'
            )
        out[key] = Slot(db_name(key), idx)
    return out


def derive_redis_url(env: dict[str, str], index: int) -> str:
    """The URL Settings assembles today, with only the DB index swapped (same scheme/auth/host)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
    from app.core.broker_connection import broker_url_from_env

    parts = urllib.parse.urlsplit(broker_url_from_env(env))
    return urllib.parse.urlunsplit(parts._replace(path=f'/{index}'))


def mask_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ''
    port = f':{parts.port}' if parts.port else ''
    cred = '***@' if parts.password or parts.username else ''
    return f'{parts.scheme}://{cred}{host}{port}{parts.path}'


# --------------------------------------------------------------------------- docker / redis seams


def _stack():
    return resolve(project=os.environ.get('COMPOSE_PROJECT_NAME') or None)


def _require_fresh(stack) -> None:
    if not stack.project.startswith(MIGRATE_ALLOWED_PREFIXES):
        raise StackError(
            WRONG_PROJECT,
            'mutation isolation provisions databases only on otfresh-* / ot-reltest-* projects',
            'use a --fresh stack: ./opentr.sh start dev --fresh <name>',
            stack.project,
        )


def _pg(pg_container: str, sql: str, dbname: str = 'postgres') -> tuple[int, str]:
    script = f'psql -U "$POSTGRES_USER" -d {shlex.quote(dbname)} -v ON_ERROR_STOP=1 -Atc {shlex.quote(sql)}'
    return container_exec(pg_container, 'sh', '-c', script)


def _redis_client(index: int):
    import redis

    return redis.Redis(
        host=os.environ.get('REDIS_HOST', 'localhost'),
        port=int(os.environ['REDIS_PORT']),
        password=os.environ.get('REDIS_PASSWORD') or None,
        db=index,
        socket_timeout=10,
    )


def _drop_create(pg: str, name: str, template: str | None = None) -> None:
    # The only DROP in this file: a name outside ot_mut_* can never reach it.
    if not DB_NAME_RE.match(name):
        raise IsolationError(f'refusing to DROP/CREATE {name!r}: not an ot_mut_* database')
    rc, out = _pg(pg, f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
    if rc != 0:
        raise IsolationError(f'DROP {name} failed: {out.strip()[:200]}')
    tmpl = f' TEMPLATE {template}' if template else ''
    rc, out = _pg(pg, f'CREATE DATABASE {name}{tmpl}')
    if rc != 0:
        raise IsolationError(f'CREATE {name} failed: {out.strip()[:200]}')


def _flush(index: int) -> None:
    if index in RESERVED_REDIS_INDEXES or index < 0:
        raise IsolationError(f'refusing to FLUSHDB reserved Redis index {index}')
    _redis_client(index).flushdb()  # FLUSHDB of ONE index; FLUSHALL is never used


def _redis_databases() -> int:
    got = _redis_client(0).config_get('databases')
    return int(got['databases'])


def _head(stack) -> str:
    from stack_checks import alembic_head

    return alembic_head(stack)


def _assert_head(pg: str, name: str, head: str) -> None:
    rc, out = _pg(pg, 'SELECT version_num FROM alembic_version', name)
    if rc != 0 or out.strip() != head:
        raise IsolationError(f'{name} is at {out.strip()[:40]!r}, want alembic head {head}')


# --------------------------------------------------------------------------- subcommands


def provision(modules: list[str]) -> dict[str, Slot]:
    stack = _stack()
    _require_fresh(stack)
    pg, backend = stack.first('postgres'), stack.first('backend')
    if not pg or not backend:
        raise IsolationError('the stack has no running postgres/backend container')
    planned = plan(modules, _redis_databases())
    head = _head(stack)
    _drop_create(pg, TEMPLATE_DB)
    # `docker exec -e` overrides the container's env_file value, so the repo's own migrate step
    # (python -m app.startup.migrate) migrates the template, never bare alembic.
    rc, out = container_exec(
        backend, 'env', f'POSTGRES_DB={TEMPLATE_DB}', 'python', '-m', 'app.startup.migrate'
    )
    if rc != 0:
        raise IsolationError(f'app.startup.migrate on {TEMPLATE_DB} failed (rc {rc}): {out[-300:]}')
    _assert_head(pg, TEMPLATE_DB, head)
    for row in planned.values():
        _drop_create(pg, str(row.db), TEMPLATE_DB)
        _assert_head(pg, str(row.db), head)
        _flush(int(row.redis_index))
    return planned


def budget(jobs: int, children: int) -> str:
    pg = _stack().first('postgres')
    rc, out = _pg(pg, 'SHOW max_connections')
    rc2, used = _pg(pg, 'SELECT count(*) FROM pg_stat_activity')
    if rc or rc2:
        raise IsolationError('cannot read max_connections / pg_stat_activity')
    maxc, in_use = int(out.strip()), int(used.strip())
    need = jobs * children * CONN_PER_CHILD + in_use + 10
    line = f'budget: need {need} (jobs {jobs} x children {children} x {CONN_PER_CHILD} + in use {in_use} + 10) vs max_connections {maxc}'
    if need > maxc:
        raise IsolationError(
            f'{line}\nFix: PG_MAX_CONNECTIONS={need + 20} ./opentr.sh start dev --fresh <name>'
            ' (or fewer --jobs)'
        )
    return line


def probe(modules: list[str]) -> list[str]:
    """Isolation probe. Each check has a positive control so a blind probe cannot pass."""
    import redis  # noqa: F401  (fail early if the client is missing)
    import sqlalchemy as sa

    planned = plan(modules, _redis_databases())
    keys = list(planned)
    if len(keys) < 2:
        raise IsolationError('the isolation probe needs at least two modules')
    a, b = keys[0], keys[1]
    log: list[str] = []

    def conn(key: str):
        url = sa.engine.URL.create(
            'postgresql+psycopg2',
            username=os.environ['POSTGRES_USER'],
            password=os.environ['POSTGRES_PASSWORD'],
            host=os.environ.get('POSTGRES_HOST', 'localhost'),
            port=int(os.environ['POSTGRES_PORT']),
            database=str(planned[key].db),
        )
        return sa.create_engine(url).connect()

    c1, c2, c3 = conn(a), conn(b), conn(a)
    try:
        k1, k2 = PROBE_LOCK_KEY
        q = sa.text('SELECT pg_try_advisory_lock(:k1, :k2)')
        if not c1.execute(q, {'k1': k1, 'k2': k2}).scalar():
            raise IsolationError('probe: could not take the advisory lock in the first database')
        if not c2.execute(q, {'k1': k1, 'k2': k2}).scalar():
            raise IsolationError(
                f'probe: advisory lock in {planned[a].db} blocked {planned[b].db}: NOT isolated'
            )
        if c3.execute(q, {'k1': k1, 'k2': k2}).scalar():
            raise IsolationError(
                'probe: positive control FAILED (a same-database lock was granted twice)'
            )
        log.append(
            f'postgres advisory lock: {planned[a].db} does not block {planned[b].db}; same-db control saw the collision'
        )
    finally:
        for c in (c1, c2, c3):
            c.close()

    token = f'ot:mutprobe:{uuid.uuid4().hex}'
    ra, rb, ra2 = (
        _redis_client(int(planned[a].redis_index)),
        _redis_client(int(planned[b].redis_index)),
        _redis_client(int(planned[a].redis_index)),
    )
    try:
        ra.set(token, 'a', ex=60)
        if rb.get(token) is not None:
            raise IsolationError(
                'probe: a key written on one Redis index is visible on another: NOT isolated'
            )
        if ra2.get(token) != b'a':
            raise IsolationError(
                'probe: positive control FAILED (same-index client cannot read the key)'
            )
        log.append(
            f'redis: index {planned[a].redis_index} key invisible on index {planned[b].redis_index}; same-index control read it'
        )
    finally:
        ra.delete(token)

    _check_settings_honour_env(planned)
    log.append('settings: DATABASE_URL and REDIS_URL honour the per-module env for every module')
    return log


def _check_settings_honour_env(planned) -> None:
    """Import app Settings in a host subprocess per module and assert it honours the isolation env.

    Settings.__init__ mkdirs TEMP_DIR (default /app/temp, a container path), which fails on the
    host, so the child gets a throwaway TEMP_DIR. The app default is deliberately unchanged.
    """
    with tempfile.TemporaryDirectory(prefix='ot-mutprobe-') as tmp:
        for key, row in planned.items():
            env = {**_child_env(key, int(row.redis_index)), 'TEMP_DIR': str(Path(tmp) / 'temp')}
            code = (
                'from urllib.parse import urlsplit;import redis;from app.core.config import settings;'
                f'assert settings.DATABASE_URL.endswith("/{row.db}"), "DATABASE_URL";'
                'kw=redis.Redis.from_url(settings.REDIS_URL).connection_pool.connection_kwargs;'
                f'assert kw["db"]=={row.redis_index}, "redis db"'
            )
            r = subprocess.run(
                [sys.executable, '-c', code],
                cwd=Path(__file__).resolve().parents[2] / 'backend',
                env={**os.environ, **env},
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode != 0:
                raise IsolationError(
                    f'probe: Settings does not honour the isolation env for {key} '
                    f'(rc={r.returncode}): {(r.stderr or r.stdout)[-2000:]}'
                )


def _child_env(key: str, index: int) -> dict[str, str]:
    return {'POSTGRES_DB': db_name(key), 'REDIS_URL': derive_redis_url(dict(os.environ), index)}


# --------------------------------------------------------------------------- verdict logs


def parse_summary(path: str) -> tuple[dict[str, str] | None, str | None]:
    """Mutant -> verdict from the LAST completed OT-MUTATION-SUMMARY block, or (None, key) if none."""
    verdicts: dict[str, str] = {}
    inside, done = False, False
    for line in Path(path).read_text(errors='replace').splitlines():
        m = SUMMARY_RE.match(line)
        if m:
            if m.group(1) == 'BEGIN':
                inside, done, verdicts = True, False, {}
            else:
                inside, done = False, True
            continue
        if inside and ': ' in line:
            name, _, verdict = line.rpartition(': ')
            verdicts[name.strip()] = verdict.strip()
    return (verdicts if done else None), None


def compare(log_a: str, log_b: str) -> tuple[int, str]:
    a, _ = parse_summary(log_a)
    b, _ = parse_summary(log_b)
    if a is None or b is None:
        return EXIT_NOT_MEASURED, 'a log lacks a completed OT-MUTATION-SUMMARY block: NOT MEASURED'
    diffs = [
        f'{n}: {a.get(n, "<absent>")} != {b.get(n, "<absent>")}'
        for n in sorted(set(a) | set(b))
        if a.get(n) != b.get(n)
    ]
    if diffs:
        return 1, '\n'.join(['verdicts differ:', *diffs])
    return 0, f'identical verdicts for {len(a)} mutants'


def judge(
    key: str, log: str, baseline_survivors: int | None, accept_improved: bool
) -> tuple[int, str]:
    """Parallel-run report. A fall in survivors is suspect (a leak 'kills' mutants), never a pass."""
    verdicts, _ = parse_summary(log)
    if verdicts is None:
        return EXIT_NOT_MEASURED, f'{key}: no completed summary'
    counts: dict[str, int] = {}
    for v in verdicts.values():
        counts[v] = counts.get(v, 0) + 1
    lines = [
        f'{key}: mutants={len(verdicts)} ' + ' '.join(f'{k}={v}' for k, v in sorted(counts.items()))
    ]
    rc = 0
    surv = counts.get('survived', 0)
    if baseline_survivors is not None and surv < baseline_survivors and not accept_improved:
        lines.append(
            f'{key}: IMPROVED UNDER --parallel ({surv} < baseline {baseline_survivors}): confirm with a serial '
            f'--module {key} before lowering the baseline (or set OT_MUTATION_PARALLEL_ACCEPT_IMPROVED=1)'
        )
        rc = EXIT_NOT_MEASURED
    if counts.get('timeout', 0) or counts.get('suspicious', 0):
        lines.append(
            f'{key}: timeouts/suspicious present: confirm serially (CPU contention is the likeliest leak)'
        )
    return rc, '\n'.join(lines)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('plan', 'provision', 'probe'):
        s = sub.add_parser(name)
        s.add_argument('--modules', required=True)
        if name == 'plan':
            s.add_argument('--redis-databases', type=int, default=16)
            s.add_argument('--tsv', action='store_true', help='key<TAB>db<TAB>index lines')
    s = sub.add_parser('budget')
    s.add_argument('--jobs', type=int, required=True)
    s.add_argument('--children', type=int, required=True)
    s = sub.add_parser('env')
    s.add_argument('--module', required=True)
    s.add_argument('--index', type=int, required=True)
    s.add_argument('--masked', action='store_true')
    s = sub.add_parser('compare')
    s.add_argument('log_a')
    s.add_argument('log_b')
    s = sub.add_parser('judge')
    s.add_argument('--module', required=True)
    s.add_argument('--log', required=True)
    s.add_argument('--baseline', type=int, default=None)
    a = p.parse_args(argv)
    try:
        if a.cmd == 'plan':
            planned = plan(a.modules.split(','), a.redis_databases)
            if a.tsv:
                print(
                    ''.join(f'{k}\t{r.db}\t{r.redis_index}\n' for k, r in planned.items()),
                    end='',
                )
            else:
                print(json.dumps({k: v._asdict() for k, v in planned.items()}, indent=1))
        elif a.cmd == 'provision':
            print(
                json.dumps(
                    {k: v._asdict() for k, v in provision(a.modules.split(',')).items()}, indent=1
                )
            )
        elif a.cmd == 'probe':
            print('\n'.join(probe(a.modules.split(','))))
        elif a.cmd == 'budget':
            print(budget(a.jobs, a.children))
        elif a.cmd == 'env':
            env = _child_env(a.module, a.index)
            if a.masked:
                print(f'POSTGRES_DB={env["POSTGRES_DB"]} REDIS_URL={mask_url(env["REDIS_URL"])}')
            else:
                print(''.join(f'export {k}={shlex.quote(v)}\n' for k, v in env.items()), end='')
        elif a.cmd == 'compare':
            rc, msg = compare(a.log_a, a.log_b)
            print(msg)
            return rc
        elif a.cmd == 'judge':
            rc, msg = judge(
                a.module,
                a.log,
                a.baseline,
                os.environ.get('OT_MUTATION_PARALLEL_ACCEPT_IMPROVED') == '1',
            )
            print(msg)
            return rc
    except IsolationError as e:
        print(f'mutation-isolation: {e}', file=sys.stderr)
        return e.code
    except StackError as e:
        print(f'mutation-isolation: {e}', file=sys.stderr)
        return e.code
    return 0


if __name__ == '__main__':
    sys.exit(main())
