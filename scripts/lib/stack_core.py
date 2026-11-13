"""Core of the stack-targeting library: model, docker/git plumbing and label-based resolution.

Public surface is re-exported by ``stack_target.py`` (the entry point). Resolution uses compose
LABELS (``docker ps --filter label=k=v`` is exact, ``name=`` is a substring match) and the ports
containers actually PUBLISH; see ``stack_target.py`` for usage and the exit-code contract.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OK, USAGE, NO_PROJECT, WRONG_PROJECT, SCHEMA, STALE_CODE = 0, 2, 20, 21, 22, 23
UNHEALTHY, MISSING_SETTING, MISSING_ASSET, MISSING_ENV, DOCKER = 24, 25, 26, 27, 28
CODE_NAMES = {
    USAGE: 'USAGE',
    NO_PROJECT: 'NO_PROJECT',
    WRONG_PROJECT: 'WRONG_PROJECT',
    SCHEMA: 'SCHEMA',
    STALE_CODE: 'STALE_CODE',
    UNHEALTHY: 'UNHEALTHY',
    MISSING_SETTING: 'MISSING_SETTING',
    MISSING_ASSET: 'MISSING_ASSET',
    MISSING_ENV: 'MISSING_ENV',
    DOCKER: 'DOCKER',
}

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_HEAD = REPO_ROOT / 'scripts' / 'release-tests' / 'lib' / 'alembic-head.py'

# (VAR, compose service, candidate container ports). Parity with opentr.sh's FRESH_*_PORT_VARS
# is policed by test_stack_target.py, so a new var there fails until it is listed here.
PORT_TABLE: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    ('FRONTEND_PORT', 'frontend', (5173, 8080)),
    ('BACKEND_PORT', 'backend', (8080,)),
    ('FLOWER_PORT', 'flower', (5555,)),
    ('POSTGRES_PORT', 'postgres', (5432,)),
    ('REDIS_PORT', 'redis', (6379,)),
    ('MINIO_PORT', 'minio', (9000,)),
    ('MINIO_CONSOLE_PORT', 'minio', (9001,)),
    ('OPENSEARCH_PORT', 'opensearch', (9200,)),
    ('OPENSEARCH_ADMIN_PORT', 'opensearch', (9600,)),
    ('DOCS_PORT', 'docs', (8080,)),
    ('KEYCLOAK_PORT', 'keycloak', (8080,)),
    ('STEP_CA_PORT', 'step-ca', (9000,)),
    ('AUTHENTIK_PORT', 'authentik-server', (9000,)),
    ('LDAP_TEST_PORT', 'lldap', (3890,)),
    ('LDAP_TEST_LDAPS_PORT', 'lldap', (6360,)),
    ('LDAP_TEST_UI_PORT', 'lldap', (17170,)),
    ('MOCK_LLM_PORT', 'mock-llm', (5199,)),
    ('MOCK_ASR_PORT', 'mock-asr', (5198,)),
    ('SMB_TEST_PORT', 'smb-test', (445,)),
    ('GRAFANA_PORT', 'grafana', (3000,)),
    ('PROMETHEUS_PORT', 'prometheus', (9090,)),
    ('ALERTMANAGER_PORT', 'alertmanager', (9093,)),
    ('LLM_TEST_PORT', 'llm-test-vllm', (8000,)),
    ('LLM_TEST_OLLAMA_PORT', 'llm-test-ollama', (11434,)),
    ('PKI_HTTPS_PORT', 'frontend', (8443,)),
    ('PKI_HTTP_PORT', 'frontend', (8080,)),
)
# Stock host ports, used only for the offset cross-check (a WARN, never a failure).
STOCK_BASE = {
    'FRONTEND_PORT': 5173,
    'BACKEND_PORT': 5174,
    'FLOWER_PORT': 5175,
    'POSTGRES_PORT': 5176,
    'REDIS_PORT': 5177,
    'MINIO_PORT': 5178,
    'MINIO_CONSOLE_PORT': 5179,
    'OPENSEARCH_PORT': 5180,
    'OPENSEARCH_ADMIN_PORT': 5181,
    'DOCS_PORT': 5183,
}

# One home for the SQL that performance-testing.md and the perf README used to spell out. The
# library list is UNQUOTED: a quoted 'a,b' is stored as one library name and postgres then fails
# to start.
PGSS_SETTINGS = (
    'ALTER SYSTEM SET shared_preload_libraries = pg_stat_statements, auto_explain',
    "ALTER SYSTEM SET pg_stat_statements.track = 'top'",
    'ALTER SYSTEM SET track_io_timing = on',
    "ALTER SYSTEM SET auto_explain.log_min_duration = '100ms'",
    'ALTER SYSTEM SET auto_explain.log_analyze = on',
    "ALTER SYSTEM SET auto_explain.log_format = 'json'",
)
SAFE_PROJECT_PREFIXES = ('otfresh-',)
MIGRATE_ALLOWED_PREFIXES = ('otfresh-', 'ot-reltest-')
LABEL_PROJECT = 'com.docker.compose.project'
LABEL_SERVICE = 'com.docker.compose.service'
LABEL_WORKDIR = 'com.docker.compose.project.working_dir'
PS_FORMAT = (
    '{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}'
    '\t{{.State}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.Ports}}'
)
DOCKER_TIMEOUT = 30


class StackError(Exception):
    """A failure with a contract exit code, a reason and a one-command fix."""

    def __init__(self, code: int, reason: str, fix: str = '', project: str = '?'):
        super().__init__(reason)
        self.code, self.reason, self.fix, self.project = code, reason, fix, project

    def line(self) -> str:
        fix = f' Fix: {self.fix}' if self.fix else ''
        name = CODE_NAMES.get(self.code, str(self.code))
        return f'stack-target: FAIL {name}({self.code}) project={self.project}: {self.reason}.{fix}'


@dataclass
class Container:
    name: str
    project: str
    service: str
    state: str
    working_dir: str
    published: dict[int, int] = field(default_factory=dict)  # container port -> host port


@dataclass
class Stack:
    project: str
    source: str
    repo_root: Path
    containers: list[Container]
    fresh_name: str | None = None
    working_dir: str = ''

    def service(self, name: str, running_only: bool = True) -> list[Container]:
        return [
            c
            for c in self.containers
            if c.service == name and (c.state == 'running' or not running_only)
        ]

    def first(self, name: str) -> str:
        found = self.service(name)
        return found[0].name if found else ''

    @property
    def ports(self) -> dict[str, int]:
        observed: dict[str, int] = {}
        for var, service, cports in PORT_TABLE:
            for container in self.service(service):
                for cport in cports:
                    if cport in container.published and var not in observed:
                        observed[var] = container.published[cport]
        return observed


# --------------------------------------------------------------------------- docker / git plumbing


def run(argv: list[str], timeout: int = DOCKER_TIMEOUT, cwd: Path | None = None) -> tuple[int, str]:
    """Run a fixed argv, returning (rc, stdout). A missing binary is rc 127."""
    try:
        done = subprocess.run(  # fixed argv, no shell  # nosec B603
            argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd
        )
    except FileNotFoundError:
        return 127, ''
    except (OSError, subprocess.SubprocessError) as exc:
        return 126, str(exc)
    return done.returncode, done.stdout


def docker(*args: str, timeout: int = DOCKER_TIMEOUT) -> str:
    """Run docker; a missing or failing docker is exit 28, never an empty answer."""
    rc, out = run(['docker', *args], timeout=timeout)
    if rc != 0:
        raise StackError(
            DOCKER,
            f'`docker {args[0]}` failed (rc {rc}); cannot judge the stack',
            'start the docker daemon / check `docker info`',
        )
    return out


def sanitize(raw: str) -> str:
    """Byte-equal to opentr.sh ``fresh_sanitize_name`` (policed by a parity test)."""
    slug = re.sub(r'[^a-z0-9]+', '-', (raw or 'default').lower()).strip('-')
    return slug or 'default'


def parse_ports(text: str) -> dict[int, int]:
    published: dict[int, int] = {}
    for match in re.finditer(r'(?:[\d.]+|\[[0-9a-f:]*\]|::):(\d+)->(\d+)/tcp', text):
        published.setdefault(int(match.group(2)), int(match.group(1)))
    return published


def list_containers(project: str | None = None) -> list[Container]:
    args = ['ps', '-a', '--format', PS_FORMAT]
    if project:
        args += ['--filter', f'label={LABEL_PROJECT}={project}']
    found: list[Container] = []
    for line in docker(*args).splitlines():
        parts = line.split('\t')
        if len(parts) < 6 or not parts[1]:
            continue  # not a compose container
        found.append(
            Container(parts[0], parts[1], parts[2], parts[3], parts[4], parse_ports(parts[5]))
        )
    return found


def inspect(name: str) -> dict:
    out = docker('inspect', name)
    try:
        parsed = json.loads(out)
        return parsed[0] if isinstance(parsed, list) and parsed else {}
    except ValueError:
        return {}


def container_exec(name: str, *cmd: str) -> tuple[int, str]:
    return run(['docker', 'exec', name, *cmd], timeout=120)


def psql(stack: Stack, sql: str) -> tuple[int, str]:
    pg = stack.first('postgres')
    if not pg:
        raise StackError(UNHEALTHY, 'no running postgres container', '', stack.project)
    script = f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc {shlex.quote(sql)}'
    return container_exec(pg, 'sh', '-c', script)


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return 0


def http_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310
            parsed = json.loads(resp.read().decode())
            return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        return {}


def git(repo: Path, *args: str) -> tuple[int, str]:
    return run(['git', '-C', str(repo), *args])


# --------------------------------------------------------------------------- resolution


def live_projects(env: dict[str, str]) -> list[str]:
    return [
        env.get('OPENTR_STOP_PROJECT_LABEL', 'opentranscribe'),
        env.get('OPENTR_STOP_PROJECT_LABEL_ALT', 'transcribe-app'),
    ]


def port_owner(port: int | str, containers: list[Container] | None = None) -> str:
    """The compose project of whichever container publishes a host port ('' if none)."""
    wanted = int(port)
    for container in containers if containers is not None else list_containers():
        if wanted in container.published.values():
            return container.project
    return ''


def project_from_env(env: dict[str, str], owners_of_port: list[str]) -> str | None:
    """Pure decision rule shared with backend/tests/compose_project.py.

    ``COMPOSE_PROJECT_NAME`` wins; otherwise the single project publishing the exported
    ``POSTGRES_PORT``. Ambiguity or silence is None: never "the only postgres anywhere", which
    could be an unrelated application's.
    """
    if env.get('COMPOSE_PROJECT_NAME'):
        return env['COMPOSE_PROJECT_NAME']
    return owners_of_port[0] if len(set(owners_of_port)) == 1 else None


def resolve(
    repo_root: Path = REPO_ROOT,
    fresh: str | None = None,
    project: str | None = None,
    stock: bool = False,
    env: dict[str, str] | None = None,
) -> Stack:
    env = dict(os.environ if env is None else env)
    if sum(bool(x) for x in (fresh, project, stock)) > 1:
        raise StackError(USAGE, 'give exactly one of --fresh, --project, --stock')
    fresh_name = sanitize(fresh) if fresh else None
    source = 'stock'
    if fresh_name:
        project, source = f'otfresh-{fresh_name}', 'fresh'
    elif project:
        source = 'project'
    elif not stock:
        port = env.get('POSTGRES_PORT', '')
        explicit = bool(env.get('COMPOSE_PROJECT_NAME'))
        owner = port_owner(port) if port.isdigit() and not explicit else ''
        project, source = project_from_env(env, [owner] if owner else []), 'env'
        if not project and port.isdigit():
            raise StackError(
                NO_PROJECT,
                f'nothing publishes the exported POSTGRES_PORT={port}',
                'start the stack, or unset POSTGRES_PORT',
            )
    if not project:
        allowed = live_projects(env)
        running = {
            c.project for c in list_containers() if c.project in allowed and c.state == 'running'
        }
        if len(running) != 1:
            raise StackError(
                NO_PROJECT,
                f'expected exactly one live project among {allowed}, found {sorted(running)}',
                'pass --fresh NAME or --project P',
            )
        project = next(iter(running))
    containers = list_containers(project)
    if not containers:
        raise StackError(
            NO_PROJECT,
            'no containers carry this compose project label',
            './opentr.sh start dev' + (f' --fresh {fresh_name}' if fresh_name else ''),
            project,
        )
    workdir = next((c.working_dir for c in containers if c.working_dir), '')
    stack = Stack(project, source, repo_root, containers, fresh_name, workdir)
    verify_ports(stack, env)
    verify_isolation(stack)
    warn_offset(stack)
    return stack


def verify_ports(stack: Stack, env: dict[str, str]) -> None:
    """Every *_PORT the caller already exported must belong to this project, never to another."""
    everything: list[Container] | None = None
    for var, _service, _cports in PORT_TABLE:
        value = env.get(var, '')
        if not value.isdigit() or int(value) in stack.ports.values():
            continue
        everything = list_containers() if everything is None else everything
        owner = port_owner(value, everything)
        if owner and owner != stack.project:
            raise StackError(
                WRONG_PROJECT,
                f"exported {var}={value} is published by project '{owner}', not '{stack.project}'",
                f"unset {var} (the resolver exports the right one) or target '{owner}'",
                stack.project,
            )


def verify_isolation(stack: Stack) -> None:
    if not stack.project.startswith(SAFE_PROJECT_PREFIXES):
        return
    for container in stack.containers:
        if not container.name.startswith(f'{stack.project}-'):
            raise StackError(
                WRONG_PROJECT,
                f"container '{container.name}' is not named {stack.project}-*",
                f'./opentr.sh stop --fresh {stack.fresh_name or stack.project[8:]} && start it again',
                stack.project,
            )
    for pg in stack.service('postgres', running_only=False):
        for mount in inspect(pg.name).get('Mounts', []):
            if (
                mount.get('Type') == 'bind'
                and mount.get('Destination') == '/var/lib/postgresql/data'
            ):
                raise StackError(
                    WRONG_PROJECT,
                    f"postgres '{pg.name}' bind-mounts a host path (live data), not the project's named volume",
                    'recreate the stack with --fresh',
                    stack.project,
                )


def warn_offset(stack: Stack) -> None:
    """An offset file is only a cross-check: .env overrides are legal and the observed port wins."""
    if not stack.fresh_name:
        return
    for base in (
        {stack.repo_root, Path(stack.working_dir)} if stack.working_dir else {stack.repo_root}
    ):
        path = base / '.fresh' / f'{stack.fresh_name}.offset'
        if not path.is_file():
            continue
        try:
            offset = int(path.read_text().strip() or '0')
        except ValueError:
            continue
        for var, port in stack.ports.items():
            if var in STOCK_BASE and port != STOCK_BASE[var] + offset:
                print(
                    f'stack-target: WARN {var}={port} != {STOCK_BASE[var]}+offset {offset} ({path}); using the observed port',
                    file=sys.stderr,
                )


def banner(stack: Stack) -> str:
    ports = stack.ports
    return (
        f'stack-target: project={stack.project} source={stack.source} working_dir={stack.working_dir or "?"} '
        f'backend=:{ports.get("BACKEND_PORT", "-")} postgres=:{ports.get("POSTGRES_PORT", "-")}'
    )


def emit_env(stack: Stack) -> str:
    ports = stack.ports
    lines = {'COMPOSE_PROJECT_NAME': stack.project, **{k: str(v) for k, v in ports.items()}}
    for var in ('MOCK_LLM_PORT', 'MOCK_ASR_PORT'):
        if var in ports:
            lines[f'OT_RESOLVED_{var}'] = str(ports[var])
    if 'FRONTEND_PORT' in ports:
        lines['E2E_FRONTEND_URL'] = f'http://localhost:{ports["FRONTEND_PORT"]}'
    if 'BACKEND_PORT' in ports:
        lines['E2E_BACKEND_URL'] = f'http://localhost:{ports["BACKEND_PORT"]}'
    lines['POSTGRES_HOST'] = 'localhost'
    backend = stack.first('backend')
    if backend:
        rc, out = container_exec(backend, 'printenv', 'REDIS_PASSWORD')
        if rc == 0 and out.strip():
            lines['REDIS_PASSWORD'] = out.strip()
    lines.update(
        ST_PROJECT=stack.project,
        ST_WORKING_DIR=stack.working_dir,
        ST_FRESH_NAME=stack.fresh_name or '',
    )
    return ''.join(f'export {k}={shlex.quote(v)}\n' for k, v in lines.items())
