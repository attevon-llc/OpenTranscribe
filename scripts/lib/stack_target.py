#!/usr/bin/env python3
"""Resolve the compose stack a script targets, and assert it is the right one.

Scripts used to work out "which stack" from accidents: a container-name substring, a hardcoded
live port, an offset file in another checkout, an unexported ``COMPOSE_PROJECT_NAME``. This module
is the one place that resolves a stack through compose LABELS and OBSERVED published ports, then
checks the things nothing used to check (schema at head, code under test, health, extensions,
gitignored assets). Stdlib only, like ``target_guard.py``.

Sources: Docker ``ps --filter label=k=v`` is an exact match while ``name=`` is a substring match
(docs.docker.com/reference/cli/docker/container/ls/#filter); compose stamps
``com.docker.compose.{project,service,project.working_dir}`` on every container
(docs.docker.com/compose/how-tos/project-name/); health is ``.State.Health.Status``
(docs.docker.com/reference/compose-file/services/#healthcheck); ``pg_stat_statements`` needs
``shared_preload_libraries`` and a restart (postgresql.org/docs/17/pgstatstatements.html);
``ALTER SYSTEM`` persists in ``postgresql.auto.conf`` (postgresql.org/docs/17/sql-altersystem.html);
Alembic keeps ``alembic_version.version_num`` (alembic.sqlalchemy.org/en/latest/tutorial.html).

Usage::

    stack_target.py resolve [--fresh NAME | --project P | --stock] [--emit-env] [--print project|json]
    stack_target.py resolve --port-owner PORT         # project publishing a host port
    stack_target.py check   [target] --require REQ [--require REQ ...] [--allow CHECK=REASON]
    stack_target.py container SERVICE [target]        # first running container of a service
    stack_target.py prepare --fresh NAME --pg-stat-statements

With no target flag: ``COMPOSE_PROJECT_NAME``, else the owner of an exported ``POSTGRES_PORT``,
else the one live project (``--stock``).

Requirements (``--require``, comma list or repeated; a ``healthy:a,b`` list keeps its commas)::

    schema-head | schema-head+migrate | code=bind[:svc,..] | code=image[:svc,..] | code=any
    healthy[:svc,..] | ext:<name> | ext:<name>+prepare | preload:<lib> | cenv:<svc>:<VAR>=<val>
    asset:<frontend-fonts|frontend-ffmpeg|nginx-ssl|pki-test-certs|env-file> | env:<VAR> | isolated

Exit codes (a stable contract; scripts/CLAUDE.md repeats it)::

    0 OK              every requirement met (overrides recorded)
    2 USAGE           bad flags, or prepare asked of a non-fresh target
    20 NO_PROJECT     the project is missing or ambiguous
    21 WRONG_PROJECT  a port owned by another project, mixed stack, not isolated, refused target
    22 SCHEMA         alembic_version differs from this checkout's head
    23 STALE_CODE     wrong checkout bind-mounted, or image sha is not HEAD with image inputs changed
    24 UNHEALTHY      a required service is not running or not healthy within the bound
    25 MISSING_SETTING extension, preload library or container env missing
    26 MISSING_ASSET  a gitignored worktree asset is absent
    27 MISSING_ENV    a required variable is unset
    28 DOCKER        docker is unreachable: cannot judge, so never a pass

One stderr line per failure: ``stack-target: FAIL <NAME>(<code>) project=<P>: <reason>. Fix: <cmd>``.
Release stages keep their own contract: ``st_require ... || exit 3``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Loaded by path (tests, python3 scripts/lib/stack_target.py): the siblings must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stack_checks import (
    check_asset,
    check_bind,
    check_cenv,
    check_code,
    check_env,
    check_ext,
    check_healthy,
    check_image,
    check_schema,
    parse_allow,
    prepare_pgss,
    run_checks,
    run_requirement,
    split_requirements,
)
from stack_core import (
    ALEMBIC_HEAD,
    CODE_NAMES,
    DOCKER,
    MIGRATE_ALLOWED_PREFIXES,
    MISSING_ASSET,
    MISSING_ENV,
    MISSING_SETTING,
    NO_PROJECT,
    OK,
    PGSS_SETTINGS,
    PORT_TABLE,
    REPO_ROOT,
    SCHEMA,
    STALE_CODE,
    STOCK_BASE,
    UNHEALTHY,
    USAGE,
    WRONG_PROJECT,
    Container,
    Stack,
    StackError,
    banner,
    container_exec,
    docker,
    emit_env,
    git,
    http_json,
    http_status,
    inspect,
    list_containers,
    live_projects,
    parse_ports,
    port_owner,
    project_from_env,
    psql,
    resolve,
    run,
    sanitize,
    verify_isolation,
    verify_ports,
    warn_offset,
)

__all__ = [
    'ALEMBIC_HEAD',
    'banner',
    'check_asset',
    'check_bind',
    'check_cenv',
    'check_code',
    'check_env',
    'check_ext',
    'check_healthy',
    'check_image',
    'check_schema',
    'CODE_NAMES',
    'Container',
    'container_exec',
    'DOCKER',
    'docker',
    'emit_env',
    'git',
    'http_json',
    'http_status',
    'inspect',
    'list_containers',
    'live_projects',
    'MIGRATE_ALLOWED_PREFIXES',
    'MISSING_ASSET',
    'MISSING_ENV',
    'MISSING_SETTING',
    'NO_PROJECT',
    'OK',
    'parse_allow',
    'parse_ports',
    'PGSS_SETTINGS',
    'port_owner',
    'PORT_TABLE',
    'prepare_pgss',
    'project_from_env',
    'psql',
    'REPO_ROOT',
    'resolve',
    'run',
    'run_checks',
    'run_requirement',
    'sanitize',
    'SCHEMA',
    'split_requirements',
    'Stack',
    'StackError',
    'STALE_CODE',
    'STOCK_BASE',
    'UNHEALTHY',
    'USAGE',
    'verify_isolation',
    'verify_ports',
    'warn_offset',
    'WRONG_PROJECT',
    'build_parser',
    'main',
]


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--repo-root', default=str(REPO_ROOT))
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('resolve', 'check', 'container', 'prepare'):
        p = sub.add_parser(name)
        p.add_argument('--fresh')
        p.add_argument('--project')
        p.add_argument('--stock', action='store_true')
        p.add_argument('--no-banner', action='store_true')
        if name in ('resolve', 'check'):
            p.add_argument('--emit-env', action='store_true')
        if name == 'resolve':
            p.add_argument('--print', dest='show', choices=('project', 'json'))
            p.add_argument('--port-owner')
        if name == 'check':
            p.add_argument('--require', action='append', default=[])
            p.add_argument('--allow', action='append', default=[])
        if name == 'container':
            p.add_argument('service')
        if name == 'prepare':
            p.add_argument('--pg-stat-statements', action='store_true', required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    repo = Path(args.repo_root)
    try:
        if args.cmd == 'resolve' and args.port_owner:
            owner = port_owner(args.port_owner)
            if owner:
                print(owner)
            return OK if owner else NO_PROJECT
        if args.cmd == 'prepare' and (args.project or args.stock or not args.fresh):
            raise StackError(USAGE, 'prepare requires --fresh NAME (never --project or --stock)')
        stack = resolve(repo, args.fresh, args.project, args.stock, env)
        if args.cmd == 'prepare':
            print(banner(stack), file=sys.stderr)
            prepare_pgss(stack)
            return OK
        if not args.no_banner:
            print(banner(stack), file=sys.stderr)
        if args.cmd == 'container':
            print(stack.first(args.service))
            return OK
        if args.cmd == 'check':
            reqs = split_requirements(args.require)
            failures = run_checks(stack, reqs, parse_allow(args.allow, env.get('ST_ALLOW', '')))
            for failure in failures:
                print(failure.line(), file=sys.stderr)
            if failures:
                return failures[0].code
        if args.cmd == 'resolve' and args.show == 'project':
            print(stack.project)
        elif args.cmd == 'resolve' and args.show == 'json':
            print(
                json.dumps(
                    {
                        'project': stack.project,
                        'source': stack.source,
                        'ports': stack.ports,
                        'working_dir': stack.working_dir,
                    }
                )
            )
        if getattr(args, 'emit_env', False):
            sys.stdout.write(emit_env(stack))
        return OK
    except StackError as exc:
        print(exc.line(), file=sys.stderr)
        return exc.code


if __name__ == '__main__':
    sys.exit(main())
