#!/usr/bin/env python3
"""Read/write a single boolean `auth_config` row, for scripts/run-dev-tests.sh (issue #630).

Bringing up the keycloak-test / ldap-test containers is necessary but not
sufficient: `test_auth_buttons.py`'s TestOIDCLogin/TestLDAPLogin classes read
`oidc_enabled`/`ldap_enabled` from the DB-backed `auth_config` table (see
`AuthConfigService.get_effective_config`, `backend/app/services/auth_config_service.py`)
and skip cleanly rather than fail when the flag reads false — so an overlay container
running with the DB flag off produces a quiet, misleading "pass" (0 real assertions),
never a loud failure. The inverse also happens: the flag can be left on from a
previous manual session while no container is running, which fails hard instead of
skipping. `run-dev-tests.sh` reconciles both directions with this helper, and restores
whatever it found on the way in — never leaves dev's auth config different from how it
found it.

Connection pattern mirrors `scripts/cleanup-test-users.py`: build the DB URL from
`.env` + dev-stack defaults directly (importing `app.core.config` has filesystem side
effects), and always force `POSTGRES_HOST=localhost` — `.env`'s `POSTGRES_HOST` names
the container-internal compose service, which does not resolve from this host process.

Usage (from repo root, inside backend venv):
    python scripts/dev-test-auth-config.py get ldap_enabled
        -> prints the stored string value, or the literal UNSET if no row exists
    python scripts/dev-test-auth-config.py set ldap_enabled true
    python scripts/dev-test-auth-config.py restore ldap_enabled UNSET
        -> deletes the row (restores "no DB override", i.e. falls back to the coded
           default) when the captured prior value was UNSET; otherwise upserts it
           back to the captured value.

Only `ldap_enabled` and `oidc_enabled` are supported — this is a narrow, single-purpose
tool for the two keys run-dev-tests.sh's auto-started overlays need, not a general
auth-config CLI (the admin UI and `PUT /api/admin/auth-config/{category}` already own
that job, and require a super-admin session this host-side script does not have).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

_env = dotenv_values(Path(__file__).resolve().parent.parent / '.env')

#: key -> category, matching AuthConfigService.set_config's convention exactly
#: (backend/app/services/auth_config_service.py) — data_type is always "bool" and
#: is_sensitive is always false for both keys this tool touches.
_KEY_CATEGORY = {
    'ldap_enabled': 'ldap',
    'oidc_enabled': 'oidc',
}

UNSET = 'UNSET'


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _env.get(name) or default


def _host_setting() -> str:
    """POSTGRES_HOST for the connection this script makes — NOT via ``_setting``.

    Same reasoning as scripts/cleanup-test-users.py: `.env`'s POSTGRES_HOST names the
    container-internal compose service, which doesn't resolve from a host process.
    """
    return os.environ.get('POSTGRES_HOST', 'localhost')


def _engine():
    db_user = _setting('POSTGRES_USER', 'postgres')
    db_password = _setting('POSTGRES_PASSWORD', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '5176')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    url = f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
    return create_engine(url)


def _validate_key(key: str) -> str:
    if key not in _KEY_CATEGORY:
        raise SystemExit(
            f'error: unsupported key {key!r} — only {sorted(_KEY_CATEGORY)} are supported'
        )
    return key


def cmd_get(engine, key: str) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text('SELECT config_value FROM auth_config WHERE config_key = :key'),
            {'key': key},
        ).first()
    print(row[0] if row is not None else UNSET)
    return 0


def cmd_set(engine, key: str, value: str) -> int:
    category = _KEY_CATEGORY[key]
    str_value = 'true' if value.strip().lower() in ('true', '1', 'yes', 'on') else 'false'
    with engine.begin() as conn:
        existing = conn.execute(
            text('SELECT id FROM auth_config WHERE config_key = :key'),
            {'key': key},
        ).first()
        if existing is None:
            conn.execute(
                text(
                    'INSERT INTO auth_config '
                    '(config_key, config_value, is_sensitive, category, data_type) '
                    "VALUES (:key, :value, false, :category, 'bool')"
                ),
                {'key': key, 'value': str_value, 'category': category},
            )
        else:
            conn.execute(
                text(
                    "UPDATE auth_config SET config_value = :value, data_type = 'bool', "
                    'category = :category WHERE config_key = :key'
                ),
                {'key': key, 'value': str_value, 'category': category},
            )
    print(f'{key}={str_value}')
    return 0


def cmd_restore(engine, key: str, prior_value: str) -> int:
    if prior_value == UNSET:
        with engine.begin() as conn:
            conn.execute(text('DELETE FROM auth_config WHERE config_key = :key'), {'key': key})
        print(f'{key} restored to UNSET (row deleted)')
        return 0
    return cmd_set(engine, key, prior_value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_get = sub.add_parser('get', help='print the current stored value, or UNSET')
    p_get.add_argument('key', choices=sorted(_KEY_CATEGORY))

    p_set = sub.add_parser('set', help='upsert a boolean value')
    p_set.add_argument('key', choices=sorted(_KEY_CATEGORY))
    p_set.add_argument('value')

    p_restore = sub.add_parser('restore', help='restore a previously captured value')
    p_restore.add_argument('key', choices=sorted(_KEY_CATEGORY))
    p_restore.add_argument('value', help='the value `get` printed before this run changed it')

    return parser


def main() -> int:
    args = build_parser().parse_args()
    key = _validate_key(args.key)
    engine = _engine()
    if args.cmd == 'get':
        return cmd_get(engine, key)
    if args.cmd == 'set':
        return cmd_set(engine, key, args.value)
    if args.cmd == 'restore':
        return cmd_restore(engine, key, args.value)
    return 2


if __name__ == '__main__':
    sys.exit(main())
