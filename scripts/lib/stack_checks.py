"""Requirement checks for a resolved stack: schema, code under test, health, settings, assets.

``run_checks`` is the one dispatcher; ``prepare_pgss`` is the only function that changes a stack
(a ``--fresh`` one, never the live or stock project). Exit codes live in ``stack_core``.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from stack_core import (
    ALEMBIC_HEAD,
    MIGRATE_ALLOWED_PREFIXES,
    MISSING_ASSET,
    MISSING_ENV,
    MISSING_SETTING,
    PGSS_SETTINGS,
    SCHEMA,
    STALE_CODE,
    UNHEALTHY,
    USAGE,
    WRONG_PROJECT,
    Stack,
    StackError,
    container_exec,
    git,
    http_json,
    http_status,
    inspect,
    list_containers,
    psql,
    run,
    verify_isolation,
)

# Seams for tests; production code never rebinds them.
_sleep = time.sleep
_now = time.monotonic

# --------------------------------------------------------------------------- checks


def _fail(stack: Stack, code: int, reason: str, fix: str = '') -> StackError:
    return StackError(code, reason, fix, stack.project)


def alembic_head(stack: Stack) -> str:
    rc, out = run([sys.executable, str(ALEMBIC_HEAD), str(stack.repo_root / 'backend')])
    if rc != 0 or not out.strip():
        raise _fail(
            stack,
            SCHEMA,
            "cannot derive this checkout's alembic head (several heads or a broken chain)",
            f'python3 {ALEMBIC_HEAD} {stack.repo_root}/backend',
        )
    return out.strip().splitlines()[-1]


def check_schema(stack: Stack, migrate: bool = False) -> None:
    if migrate:
        if not stack.project.startswith(MIGRATE_ALLOWED_PREFIXES):
            raise _fail(
                stack,
                WRONG_PROJECT,
                'schema-head+migrate is refused outside otfresh-* and ot-reltest-* projects',
                'use plain schema-head, or target a --fresh stack',
            )
        backend = stack.first('backend')
        rc, _ = (
            container_exec(backend, 'python', '-m', 'app.startup.migrate') if backend else (1, '')
        )
        if rc != 0:
            raise _fail(
                stack,
                SCHEMA,
                f'`python -m app.startup.migrate` failed (rc {rc})',
                'read ./opentr.sh logs backend',
            )
    want = alembic_head(stack)
    rc, out = psql(stack, 'SELECT version_num FROM alembic_version')
    rows = [r for r in out.splitlines() if r.strip()]
    if rc != 0 or len(rows) != 1:
        raise _fail(
            stack,
            SCHEMA,
            f'alembic_version is unreadable or has {len(rows)} rows (want exactly 1)',
            'migrate the stack: ./opentr.sh restart-backend',
        )
    got = rows[0].strip()
    if got == want:
        return
    behind = (
        run(
            [
                sys.executable,
                str(ALEMBIC_HEAD),
                str(stack.repo_root / 'backend'),
                '--has-revision',
                got,
            ]
        )[0]
        == 0
    )
    where = 'behind' if behind else 'ahead/foreign'
    raise _fail(
        stack,
        SCHEMA,
        f"schema {where}: database is at {got}, this checkout's head is {want}",
        'migrate to head (build_snapshot.sh, or `python -m app.startup.migrate` in the backend)',
    )


def check_bind(stack: Stack, services: list[str]) -> None:
    for svc in services:
        containers = stack.service(svc)
        if not containers:
            raise _fail(
                stack, STALE_CODE, f"no running '{svc}' container to read the /app mount from", ''
            )
        wanted = (stack.repo_root / svc).resolve()
        sources = [
            os.path.realpath(m.get('Source', ''))
            for m in inspect(containers[0].name).get('Mounts', [])
            if m.get('Type') == 'bind' and m.get('Destination') == '/app'
        ]
        if str(wanted) not in sources:
            raise _fail(
                stack,
                STALE_CODE,
                f'{svc} serves {sources or "BAKED image code"}, not {wanted}',
                'restart the stack from this checkout: ./opentr.sh start dev',
            )


def _sha_ok(repo: Path, sha: str) -> str:
    """Return '' when `sha` is acceptable for HEAD, else the reason."""
    if not sha or sha == 'unknown' or sha.endswith('-dirty'):
        return f"image revision '{sha}' is unattributable"
    head = git(repo, 'rev-parse', '--short', 'HEAD')[1].strip()
    if head and (head.startswith(sha) or sha.startswith(head)):
        return ''
    if git(repo, 'merge-base', '--is-ancestor', sha, 'HEAD')[0] != 0:
        return f'image revision {sha} is not an ancestor of HEAD {head}'
    changed = git(
        repo,
        'diff',
        '--quiet',
        sha,
        'HEAD',
        '--',
        'backend',
        'frontend',
        'docs-site',
        ':(exclude)backend/tests',
    )[0]
    return (
        ''
        if changed == 0
        else f'image revision {sha} predates changes under backend/frontend/docs-site since (HEAD {head})'
    )


def check_image(stack: Stack, services: list[str]) -> None:
    for svc in services:
        containers = stack.service(svc)
        if not containers:
            raise _fail(stack, STALE_CODE, f"no running '{svc}' container", '')
        name = containers[0].name
        rc, out = container_exec(name, 'printenv', 'GIT_SHA')
        problem = _sha_ok(stack.repo_root, out.strip() if rc == 0 else '')
        if not problem:
            label = (
                inspect(name)
                .get('Config', {})
                .get('Labels', {})
                .get('org.opencontainers.image.revision', '')
            )
            problem = _sha_ok(stack.repo_root, label[:12]) if label else ''
        port = stack.ports.get('BACKEND_PORT')
        if not problem and svc == 'backend' and port:
            served = str(http_json(f'http://localhost:{port}/api/version').get('git_sha', ''))
            problem = _sha_ok(stack.repo_root, served) if served else ''
        if problem:
            raise _fail(
                stack,
                STALE_CODE,
                f'{svc}: {problem}',
                'rebuild from this checkout: ./opentr.sh rebuild-backend (or start dev --fresh NAME --build)',
            )


def check_code(stack: Stack, mode: str, services: list[str]) -> None:
    if mode == 'bind':
        return check_bind(stack, services or ['backend', 'frontend'])
    if mode == 'image':
        return check_image(stack, services or ['backend'])
    try:
        check_bind(stack, services or ['backend'])
    except StackError:
        check_image(stack, services or ['backend'])


def _container_health(name: str) -> tuple[str, str, int, str]:
    info = inspect(name)
    state = info.get('State', {})
    health = state.get('Health', {}).get('Status', '')
    policy = info.get('HostConfig', {}).get('RestartPolicy', {}).get('Name', '')
    return state.get('Status', ''), health, int(state.get('ExitCode', 0) or 0), policy


def check_healthy(stack: Stack, services: list[str], timeout: float | None = None) -> None:
    timeout = float(os.environ.get('ST_HEALTH_TIMEOUT', '300')) if timeout is None else timeout
    deadline = _now() + timeout
    for svc in services or ['postgres', 'redis', 'backend']:
        while True:
            fresh = [c for c in list_containers(stack.project) if c.service == svc]
            if not fresh:
                raise _fail(
                    stack, UNHEALTHY, f"service '{svc}' has no container", './opentr.sh start dev'
                )
            waiting = False
            for container in fresh:
                status, health, exit_code, policy = _container_health(container.name)
                if status == 'exited' and exit_code == 0 and policy in ('no', ''):
                    continue  # a finished one-shot (migrate)
                if status != 'running' or health == 'unhealthy':
                    raise _fail(
                        stack,
                        UNHEALTHY,
                        f'{container.name} is {status}/{health or "no healthcheck"}',
                        f'./opentr.sh logs {svc}',
                    )
                waiting = waiting or health == 'starting'
            if not waiting:
                break
            if _now() >= deadline:
                raise _fail(
                    stack,
                    UNHEALTHY,
                    f"{svc} still 'starting' after {timeout:g}s (ST_HEALTH_TIMEOUT)",
                    f'./opentr.sh logs {svc}',
                )
            _sleep(2)
    port = stack.ports.get('BACKEND_PORT')
    if port and 'backend' in (services or ['postgres', 'redis', 'backend']):
        streak = 0
        while streak < 3:
            streak = (
                streak + 1 if http_status(f'http://localhost:{port}/health/ready') == 200 else 0
            )
            if streak >= 3:
                break
            if _now() >= deadline:
                raise _fail(
                    stack,
                    UNHEALTHY,
                    f'backend :{port}/health/ready did not return 3 consecutive 200s',
                    './opentr.sh logs backend',
                )
            _sleep(1)


def check_ext(stack: Stack, name: str, preload_only: bool = False) -> None:
    rc, libs = psql(stack, 'SHOW shared_preload_libraries')
    if rc != 0 or name not in [x.strip() for x in libs.strip().split(',')]:
        raise _fail(
            stack,
            MISSING_SETTING,
            f"{name} is not in shared_preload_libraries ('{libs.strip()}')",
            'python3 scripts/lib/stack_target.py prepare --fresh NAME --pg-stat-statements',
        )
    if preload_only:
        return
    if name == 'pg_stat_statements' and psql(stack, 'SHOW compute_query_id')[1].strip() == 'off':
        raise _fail(
            stack,
            MISSING_SETTING,
            'compute_query_id is off, so pg_stat_statements records nothing',
            '',
        )
    if not psql(stack, f"SELECT 1 FROM pg_extension WHERE extname='{name}'")[1].strip():
        raise _fail(
            stack,
            MISSING_SETTING,
            f'extension {name} is not created in this database',
            f'CREATE EXTENSION IF NOT EXISTS {name}',
        )


def check_cenv(stack: Stack, spec: str) -> None:
    svc, _, rest = spec.partition(':')
    var, _, want = rest.partition('=')
    name = stack.first(svc)
    rc, out = container_exec(name, 'printenv', var) if name else (1, '')
    if rc != 0 or out.strip() != want:
        raise _fail(
            stack,
            MISSING_SETTING,
            f'{svc} has {var}={out.strip() if rc == 0 else "<unset>"}, want {want}',
            f"restart the stack with {var}'s source exported (compose reads it only at `up`)",
        )


def asset_root(stack: Stack) -> Path:
    for m in inspect(stack.first('frontend')).get('Mounts', []) if stack.first('frontend') else []:
        if m.get('Type') == 'bind' and m.get('Destination') == '/app':
            return Path(m['Source']).parent
    return stack.repo_root


def check_asset(stack: Stack, name: str) -> None:
    root = asset_root(stack)
    registry: dict[str, tuple[list[Path], str]] = {
        'frontend-fonts': (
            sorted((root / 'frontend/static/fonts').glob('*.woff2')),
            'cd frontend && npm run setup-fonts',
        ),
        'frontend-ffmpeg': (
            sorted((root / 'frontend/static/ffmpeg').glob('*')),
            'cd frontend && npm run setup-ffmpeg',
        ),
        'nginx-ssl': (
            [
                p
                for p in (root / 'nginx/ssl/server.crt', root / 'nginx/ssl/server.key')
                if p.is_file()
            ],
            'scripts/generate-ssl-cert.sh',
        ),
        'pki-test-certs': (
            sorted((root / 'scripts/pki/test-certs').glob('*')),
            'scripts/pki/generate-test-env.sh',
        ),
        'env-file': ([p for p in (root / '.env',) if p.exists()], 'cp .env.example .env'),
    }
    if name not in registry:
        raise StackError(
            USAGE, f"unknown asset '{name}' (known: {sorted(registry)})", '', stack.project
        )
    files, fix = registry[name]
    needed = 2 if name == 'nginx-ssl' else 1
    if len([f for f in files if f.stat().st_size > 0]) < needed:
        raise _fail(stack, MISSING_ASSET, f"asset '{name}' is missing or empty under {root}", fix)


def check_env(stack: Stack, var: str) -> None:
    if os.environ.get(var):
        return
    env_file = stack.repo_root / '.env'
    if env_file.is_file():
        pattern = re.compile(rf'^\s*(?:export\s+)?{re.escape(var)}\s*=\s*[^\s#]', re.MULTILINE)
        if pattern.search(env_file.read_text(errors='replace')):
            return  # present; the value is never printed
    raise _fail(
        stack,
        MISSING_ENV,
        f'{var} is unset in the environment and absent from .env',
        f'export {var}=... (see .env.example)',
    )


def prepare_pgss(stack: Stack) -> None:
    """Load pg_stat_statements on a fresh stack: ALTER SYSTEM, restart, extension. Idempotent."""
    if not stack.project.startswith('otfresh-'):
        raise _fail(
            stack, WRONG_PROJECT, 'prepare only touches otfresh-* stacks', 'use --fresh NAME'
        )
    try:
        check_ext(stack, 'pg_stat_statements')
        return
    except StackError as exc:
        if exc.code != MISSING_SETTING:
            raise
    for statement in PGSS_SETTINGS:
        rc, out = psql(stack, statement)
        if rc != 0:
            raise _fail(stack, MISSING_SETTING, f'`{statement}` failed: {out.strip()[:200]}', '')
    opentr = str(stack.repo_root / 'opentr.sh')
    name = stack.fresh_name or ''
    if run([opentr, 'restart-db', '--fresh', name], timeout=600, cwd=stack.repo_root)[0] != 0:
        raise _fail(stack, UNHEALTHY, '`opentr.sh restart-db` failed', './opentr.sh logs postgres')
    check_healthy(stack, ['postgres'])
    if run([opentr, 'restart-backend', '--fresh', name], timeout=900, cwd=stack.repo_root)[0] != 0:
        raise _fail(
            stack, UNHEALTHY, '`opentr.sh restart-backend` failed', './opentr.sh logs backend'
        )
    stack.containers = list_containers(stack.project)
    check_healthy(stack, ['postgres', 'redis', 'backend'])
    psql(stack, 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
    check_ext(stack, 'pg_stat_statements')


KNOWN_PREFIXES = (
    'schema-head',
    'code=',
    'healthy',
    'ext:',
    'preload:',
    'cenv:',
    'asset:',
    'env:',
    'isolated',
)


def split_requirements(raw: list[str]) -> list[str]:
    """Split comma lists, gluing a token that is not a requirement onto the one before it."""
    out: list[str] = []
    for chunk in raw:
        for token in chunk.split(','):
            if not token:
                continue
            if token.startswith(KNOWN_PREFIXES) or not out:
                out.append(token)
            else:
                out[-1] += ',' + token
    return out


def parse_allow(items: list[str], env_value: str) -> dict[str, str]:
    allow: dict[str, str] = {}
    for item in [*items, *[x for x in env_value.split(';') if x]]:
        key, sep, reason = item.partition('=')
        if not sep or not reason.strip():
            raise StackError(USAGE, f"--allow needs CHECK=REASON, got '{item}'")
        allow[key.strip()] = reason.strip()
    return allow


def run_requirement(stack: Stack, req: str) -> None:
    key = re.split(r'[:=+]', req, maxsplit=1)[0]
    head, _, arg = req.partition(':')
    svcs = [s for s in arg.split(',') if s]
    if req == 'schema-head':
        check_schema(stack)
    elif req == 'schema-head+migrate':
        check_schema(stack, migrate=True)
    elif req.startswith('code='):
        mode, _, tail = req[5:].partition(':')
        if mode not in ('bind', 'image', 'any'):
            raise StackError(USAGE, f"unknown code mode '{mode}'", '', stack.project)
        check_code(stack, mode, [s for s in tail.split(',') if s])
    elif head == 'healthy':
        check_healthy(stack, svcs)
    elif head in ('ext', 'preload'):
        name, plus, _ = arg.partition('+')
        try:
            check_ext(stack, name, preload_only=head == 'preload')
        except StackError:
            if not plus:
                raise
            prepare_pgss(stack)
    elif head == 'cenv':
        check_cenv(stack, arg)
    elif head == 'asset':
        check_asset(stack, arg)
    elif head == 'env':
        check_env(stack, arg)
    elif req == 'isolated':
        if not stack.project.startswith('otfresh-'):
            raise _fail(
                stack,
                WRONG_PROJECT,
                'not an otfresh-* project, so isolation cannot be shown',
                'use --fresh NAME',
            )
        verify_isolation(stack)
    else:
        raise StackError(USAGE, f"unknown requirement '{req}' (key {key})", '', stack.project)


def run_checks(stack: Stack, reqs: list[str], allow: dict[str, str]) -> list[StackError]:
    failures: list[StackError] = []
    for req in reqs:
        key = re.split(r'[:=]', req, maxsplit=1)[0]
        try:
            run_requirement(stack, req)
        except StackError as exc:
            reason = allow.get(req) or allow.get(key) or allow.get(key.split('+')[0])
            if reason and exc.code != USAGE:
                print(f'stack-target: OVERRIDDEN {req} ({reason})', file=sys.stderr)
                continue
            failures.append(exc)
    return failures
