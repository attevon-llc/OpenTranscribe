#!/usr/bin/env python3
"""Signature-scoped cleanup of orphaned test data (issue #629).

``scripts/cleanup-test-users.py`` only ever swept user rows (and, later, leaked LLM
configs) — every OTHER kind of test-minted data (uploaded media, collections, tags,
watch sources, speaker profiles, chat conversations) had no sweep at all and
accumulated silently on the shared dev stack. This script is the sibling that covers
the rest, and is the ONE entry point a caller should invoke — it shells out to
``cleanup-test-users.py`` for the user/LLM-config plane (same idiom
``run-integration-tests.sh`` already uses) rather than duplicating that logic.

Why a media sweep cannot be pure SQL: child rows (``transcript_segment``,
``analytics``, ``comment``, ``task``) reference ``media_file.id`` with
``ON DELETE NO ACTION``, and MinIO objects + OpenSearch documents (transcript index,
``transcript_chunks``, speaker embeddings) are unreachable from Postgres at all. The
single canonical destroyer is ``purge_media_file()``
(``backend/app/services/file_cleanup_service.py``), reached only via
``DELETE /api/files/{uuid}`` (falling back to ``/force``). So candidates are read
from Postgres (authoritative, sees every owner, no pagination) and deletions are
issued over the HTTP API — for media AND for every other Tier A resource type below,
so cascade/embedding cleanup on each resource's own delete path always runs.

Safety tiers (see the issue #629 plan for the full reasoning):

* **Tier A (unambiguous)** — filename/name/title shapes with a random hex suffix, or
  an RFC 2606 ``.invalid`` email TLD. No human types one of these by hand. Selected by
  ``--execute-unambiguous``.
* **Tier B (review)** — plausible hand-created names (``testuser_%``,
  ``test-%@example.com``). Deletion needs the full ``--execute``.
* **Tier C** — report-count only, never deleted here: OpenSearch orphans (should not
  accumulate once every media delete routes through the API) and ``refresh_token``
  growth (a separate bug — see the issue this script's docstring references).

A run's own data is distinguished from a concurrently LIVE run's data by TIME, not by
name — see ``scripts/testrun-registry.sh``. Every candidate table already carries a
creation timestamp, so the sweep cutoff is ``min(started_at)`` across every currently
live run marker (including this run's own), and only rows older than that are ever
touched.

Usage (from repo root, inside backend venv):
    python scripts/cleanup-test-data.py                        # dry run, all tiers reported
    python scripts/cleanup-test-data.py --execute-unambiguous   # delete Tier A only
    python scripts/cleanup-test-data.py --execute               # delete Tier A + Tier B
    python scripts/cleanup-test-data.py --json                  # machine-readable report
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json as json_module
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import requests
from dotenv import dotenv_values
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).resolve().parent.parent
# TESTRUN_REGISTRY_DIR overrides this — same override name testrun-registry.sh honours,
# used only by backend/tests/integration/test_cleanup_test_data_isolated.py so that test
# never reads (or is affected by) the real repo's .testruns/.
_TESTRUNS_DIR = Path(os.environ.get('TESTRUN_REGISTRY_DIR', str(_REPO_ROOT / '.testruns')))
_CLEANUP_USERS_SCRIPT = _REPO_ROOT / 'scripts' / 'cleanup-test-users.py'

_env = dotenv_values(_REPO_ROOT / '.env')


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _env.get(name) or default


def _host_setting() -> str:
    """See ``cleanup-test-users.py``'s identical helper — same reasoning, duplicated
    rather than imported, so this script has no import-time dependency on that one
    (it invokes it as a subprocess instead, at the very end of the sweep)."""
    return os.environ.get('POSTGRES_HOST', 'localhost')


def _backend_url() -> str:
    return os.environ.get('E2E_BACKEND_URL', 'http://localhost:5174')


def _admin_email() -> str:
    return os.environ.get('E2E_ADMIN_EMAIL', 'admin@example.com')


def _admin_password() -> str:
    return os.environ.get('E2E_ADMIN_PASSWORD', 'password')  # noqa: S107 — documented dev default


def _load_cleanup_users_module() -> ModuleType:
    """Load ``cleanup-test-users.py`` by path (hyphenated filename blocks a normal
    import) — same pattern as its own unit test's ``_load_script``."""
    spec = importlib.util.spec_from_file_location('cleanup_test_users', _CLEANUP_USERS_SCRIPT)
    assert spec is not None and spec.loader is not None, f'cannot load {_CLEANUP_USERS_SCRIPT}'
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------------------------
# Liveness cutoff (Decision 4) — pure functions, no I/O side effects beyond marker reads.
# ---------------------------------------------------------------------------------------------

_STARTED_AT_RE = re.compile(r'started_at=(\d+)')


def _read_started_at(marker: Path) -> int | None:
    try:
        content = marker.read_text(encoding='utf-8')
    except OSError:
        return None
    match = _STARTED_AT_RE.search(content)
    return int(match.group(1)) if match else None


def live_marker_start_times(testruns_dir: Path) -> list[int]:
    """``started_at`` epoch seconds for every LIVE run marker under *testruns_dir*.

    A marker is live iff its ``flock`` is currently held by another process, probed
    non-blockingly so this call never waits. If probing SUCCEEDS in acquiring the
    lock, nobody was holding it — the run that created it is gone (crashed, or exited
    without ever removing the file) — so it is stale and excluded, and the lock is
    released again immediately (this process does not want to hold it).
    """
    if not testruns_dir.is_dir():
        return []
    live: list[int] = []
    for marker in sorted(testruns_dir.glob('*.lock')):
        try:
            fd = os.open(str(marker), os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Could not acquire -> someone else holds it -> live.
            started_at = _read_started_at(marker)
            if started_at is not None:
                live.append(started_at)
        else:
            # Acquired it ourselves -> nobody was holding it -> stale.
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return live


def resolve_cutoff(live_starts: list[int], *, now: int | None = None) -> int:
    """The sweep cutoff: the oldest currently-live run's start time, or now if none.

    Pure function — no I/O — so it is trivially unit-testable. A row created BEFORE
    this cutoff belongs to no currently-live run and is safe to consider; a row
    created at or after it might belong to a run still in progress and must be left
    alone. ``now`` always participates (a caller's own just-registered marker
    included), so a fresh, single, isolated run still gets a sane (very recent)
    cutoff rather than an unbounded one.
    """
    resolved_now = now if now is not None else int(time.time())
    return min([*live_starts, resolved_now])


# ---------------------------------------------------------------------------------------------
# Tier A candidate signatures
# ---------------------------------------------------------------------------------------------


class MediaFilenameSpec(NamedTuple):
    label: str
    sql_prefix: str  # coarse LIKE pre-filter, escaped for `ESCAPE '\'`
    shape: re.Pattern[str]  # precise shape check applied in Python


#: Coarse SQL pre-filter is intentionally loose (a bare prefix); the SHAPE regex is what
#: actually decides membership, so a human's ``reprocess-notes.wav`` is filtered out even
#: though it shares the SQL LIKE prefix with a real fixture upload.
MEDIA_FILENAME_SPECS: list[MediaFilenameSpec] = [
    MediaFilenameSpec(
        'owned', r'e2e-owned-%', re.compile(r'^e2e-owned-[0-9a-f]{8}\.[A-Za-z0-9]+$')
    ),
    MediaFilenameSpec('upload', r'e2e\_upload\_%', re.compile(r'^e2e_upload_\d+_\d+\.wav$')),
    MediaFilenameSpec(
        'reprocess', r'reprocess-%', re.compile(r'^reprocess-[0-9a-f]{8}\.[A-Za-z0-9]+$')
    ),
    MediaFilenameSpec(
        'gpu-scale-smoke',
        r'gpu-scale-smoke-%',
        re.compile(r'^gpu-scale-smoke-\d+-\d+\.wav$'),
    ),
]

#: name/title-prefix + <8hex> shape, one entry per non-media resource type.
NAME_PREFIX_SPECS: dict[str, tuple[str, str]] = {
    # label: (table, column)
    'collection': ('collection', 'name'),
    'tag': ('tag', 'name'),
    'watch_source': ('watch_source', 'name'),
    'speaker_profile': ('speaker_profile', 'name'),
    'chat_conversation': ('chat_conversation', 'title'),
}

NAME_PREFIXES: dict[str, list[str]] = {
    'collection': ['e2e-shared-', 'e2e-collection-'],
    'tag': ['e2e-tag-'],
    'watch_source': ['e2e-watch-'],
    'speaker_profile': ['e2e-gender-'],
    'chat_conversation': ['e2e-chat-'],
}

_HEX8_SHAPE = re.compile(r'[0-9a-f]{8}')


def _name_matches_shape(name: str, prefixes: list[str]) -> bool:
    """A registered prefix followed somewhere by an 8-hex-char uniquifier — never a
    bare prefix match, so a human-created ``e2e-tag-notes`` (no hex suffix) survives.
    """
    for prefix in prefixes:
        if name.startswith(prefix) and _HEX8_SHAPE.search(name[len(prefix) :]):
            return True
    return False


# ---------------------------------------------------------------------------------------------
# Candidate discovery (Postgres — read-only)
# ---------------------------------------------------------------------------------------------


class Candidate(NamedTuple):
    resource: str  # "media_file" | "collection" | "tag" | "watch_source" | "speaker_profile"
    #                 | "chat_conversation"
    uuid: str
    label: str  # filename / name / title, for reporting
    owner_email: str | None


def _engine_url() -> str:
    db_user = _setting('POSTGRES_USER', 'postgres')
    db_password = _setting('POSTGRES_PASSWORD', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '5176')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    return f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'


def find_media_by_filename(conn: Any, cutoff: int) -> list[Candidate]:
    found: list[Candidate] = []
    for spec in MEDIA_FILENAME_SPECS:
        rows = conn.execute(
            text(
                """
                SELECT m.uuid, m.filename, u.email
                FROM media_file m
                JOIN "user" u ON u.id = m.user_id
                WHERE m.filename LIKE :pattern ESCAPE '\\'
                  AND m.upload_time < to_timestamp(:cutoff)
                """
            ),
            {'pattern': spec.sql_prefix, 'cutoff': cutoff},
        ).fetchall()
        for row in rows:
            filename = row.filename or ''
            if spec.shape.match(filename):
                found.append(Candidate('media_file', str(row.uuid), filename, row.email))
    return found


def find_media_by_tier_a_owner(
    conn: Any, cutoff: int, unambiguous_email_patterns: list[str]
) -> list[Candidate]:
    """Media owned by a user matching a Tier-A (unambiguous) email pattern.

    Closes the corpus-owner gap (Decision 3.2): a user with ``files > 0`` is never a
    deletion candidate in ``cleanup-test-users.py``'s own sweep, so the search-quality
    corpus owner's files would otherwise never be reachable at all. Sweeping THESE
    first (before the user sweep runs) is what lets that user become deletable later
    in the same pass.
    """
    if not unambiguous_email_patterns:
        return []
    where = ' OR '.join(f"u.email LIKE '{p}' ESCAPE '\\'" for p in unambiguous_email_patterns)
    rows = conn.execute(
        text(
            f"""
            SELECT m.uuid, m.filename, u.email
            FROM media_file m
            JOIN "user" u ON u.id = m.user_id
            WHERE ({where})
              AND m.upload_time < to_timestamp(:cutoff)
            """
        ),
        {'cutoff': cutoff},
    ).fetchall()
    return [Candidate('media_file', str(row.uuid), row.filename or '', row.email) for row in rows]


def find_named_resources(conn: Any, cutoff: int) -> dict[str, list[Candidate]]:
    results: dict[str, list[Candidate]] = {}
    for resource, (table, column) in NAME_PREFIX_SPECS.items():
        time_col = 'upload_time' if table == 'media_file' else 'created_at'
        rows = conn.execute(
            text(
                f"""
                SELECT r.uuid, r.{column} AS label, u.email
                FROM "{table}" r
                LEFT JOIN "user" u ON u.id = r.user_id
                WHERE r.{time_col} < to_timestamp(:cutoff)
                """
            ),
            {'cutoff': cutoff},
        ).fetchall()
        matches = [
            Candidate(resource, str(row.uuid), row.label or '', row.email)
            for row in rows
            if row.label and _name_matches_shape(row.label, NAME_PREFIXES[resource])
        ]
        results[resource] = matches
    return results


# ---------------------------------------------------------------------------------------------
# API deletion plane
# ---------------------------------------------------------------------------------------------

_DELETE_PATH_BY_RESOURCE = {
    'media_file': '/api/files/{uuid}',
    'collection': '/api/collections/{uuid}',
    'tag': None,  # bulk endpoint, handled separately (query-param list)
    'watch_source': '/api/watch-sources/{uuid}',
    'speaker_profile': '/api/speaker-profiles/profiles/{uuid}',
    'chat_conversation': '/api/chat/conversations/{uuid}',
}


class ApiSession:
    """A thin, best-effort wrapper around the admin bearer token + CSRF cookie.

    Degrades rather than raises: if login fails, ``session`` is ``None`` and every
    caller checks that before attempting a delete — the DB-only planes (LLM configs,
    users, via ``cleanup-test-users.py``) still run.
    """

    def __init__(self, backend_url: str) -> None:
        self.backend_url = backend_url
        self.session: requests.Session | None = None

    def login(self) -> bool:
        session = requests.Session()
        try:
            resp = session.post(
                f'{self.backend_url}/api/auth/token',
                data={'username': _admin_email(), 'password': _admin_password()},
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f'  WARN  could not reach backend at {self.backend_url}: {exc}')
            return False
        if resp.status_code != 200:
            print(f'  WARN  admin login failed: HTTP {resp.status_code}')
            return False
        csrf = session.cookies.get('csrf_token')
        if csrf:
            session.headers['X-CSRF-Token'] = csrf
        self.session = session
        return True

    def logout(self) -> None:
        """Courtesy fix (Decision 6): this tool must not itself contribute to the
        refresh_token leak it is adjacent to."""
        if self.session is None:
            return
        with contextlib.suppress(requests.RequestException):
            self.session.post(f'{self.backend_url}/api/auth/logout', timeout=15)

    def delete_media_file(self, file_uuid: str) -> bool:
        """Mirrors ``e2e/conftest.py``'s ``delete_media_file``: wait out an
        ``active_task_id``, DELETE, fall back to ``/force`` on persistent 409."""
        assert self.session is not None
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                detail = self.session.get(f'{self.backend_url}/api/files/{file_uuid}', timeout=30)
                if detail.status_code == 200 and detail.json().get('active_task_id') is not None:
                    time.sleep(3)
                    continue
            except requests.RequestException:
                pass
            break
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                resp = self.session.delete(f'{self.backend_url}/api/files/{file_uuid}', timeout=30)
                if resp.status_code in (200, 204, 404):
                    return True
            except requests.RequestException:
                pass
            time.sleep(5)
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/files/{file_uuid}/force', timeout=30
            )
            return resp.status_code in (200, 204, 404)
        except requests.RequestException:
            return False

    def delete_simple(self, resource: str, resource_uuid: str) -> bool:
        assert self.session is not None
        path_template = _DELETE_PATH_BY_RESOURCE[resource]
        assert path_template is not None, f'{resource} has no simple delete path'
        url = f'{self.backend_url}{path_template.format(uuid=resource_uuid)}'
        try:
            resp = self.session.delete(url, timeout=30)
            return resp.status_code in (200, 204, 404)
        except requests.RequestException:
            return False

    def delete_tags(self, tag_uuids: list[str]) -> bool:
        assert self.session is not None
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/tags',
                params=[('tag_uuids', u) for u in tag_uuids],
                timeout=30,
            )
            return resp.ok
        except requests.RequestException:
            return False


# ---------------------------------------------------------------------------------------------
# Reporting / main
# ---------------------------------------------------------------------------------------------


def _delete_candidates(
    api: ApiSession, resource: str, candidates: list[Candidate]
) -> tuple[int, bool]:
    """Issue the real deletes for one resource type. Returns ``(deleted_count, any_failed)``.

    The ONLY place in this module that calls ``api.delete_media_file`` /
    ``api.delete_simple`` / ``api.delete_tags`` — callers must reach this function
    through the ``if effective_execute and api_available:`` guard in ``main()``.
    """
    deleted = 0
    any_failed = False
    if resource == 'tag':
        if candidates and api.delete_tags([c.uuid for c in candidates]):
            deleted = len(candidates)
        elif candidates:
            any_failed = True
    elif resource == 'media_file':
        for cand in candidates:
            if api.delete_media_file(cand.uuid):
                deleted += 1
            else:
                any_failed = True
    else:
        for cand in candidates:
            if api.delete_simple(resource, cand.uuid):
                deleted += 1
            else:
                any_failed = True
    return deleted, any_failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--execute-unambiguous', action='store_true', help='Delete Tier A candidates only'
    )
    parser.add_argument(
        '--execute', action='store_true', help='Delete Tier A + Tier B (review) candidates'
    )
    parser.add_argument('--json', action='store_true', help='Machine-readable aggregate report')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    effective_execute = args.execute or args.execute_unambiguous

    backend_url = _backend_url()
    db_user = _setting('POSTGRES_USER', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '5176')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    print(f'Target: {db_user}@{db_host}:{db_port}/{db_name} + API {backend_url}')

    cleanup_users = _load_cleanup_users_module()
    live_starts = live_marker_start_times(_TESTRUNS_DIR)
    cutoff = resolve_cutoff(live_starts)
    print(
        f'Cutoff: rows created before {cutoff} ({len(live_starts)} concurrently live '
        f'run marker(s) found)'
    )

    engine = create_engine(_engine_url())
    report: dict[str, Any] = {'cutoff': cutoff, 'resources': {}}
    exit_code = 0

    with engine.connect() as conn:
        media_candidates = find_media_by_filename(conn, cutoff)
        media_candidates += find_media_by_tier_a_owner(
            conn, cutoff, cleanup_users.ORPHAN_PATTERNS_UNAMBIGUOUS
        )
        # De-dupe: a Tier-A-owned file could also match a filename spec.
        seen_uuids: set[str] = set()
        deduped_media: list[Candidate] = []
        for cand in media_candidates:
            if cand.uuid not in seen_uuids:
                seen_uuids.add(cand.uuid)
                deduped_media.append(cand)
        media_candidates = deduped_media

        named = find_named_resources(conn, cutoff)

    all_candidates: dict[str, list[Candidate]] = {'media_file': media_candidates, **named}

    api = ApiSession(backend_url)
    api_available = api.login()
    if not api_available:
        print('  WARN  API plane unavailable — skipping media/collection/tag/watch-source/')
        print('        speaker-profile/conversation deletion this run (DB-only planes still run)')
        exit_code = 1

    for resource, candidates in all_candidates.items():
        print(f'\n{resource}: {len(candidates)} Tier A candidate(s)')
        report['resources'][resource] = {'count': len(candidates), 'deleted': 0}
        for cand in candidates:
            verb = 'DELETE' if (effective_execute and api_available) else 'WOULD DELETE'
            print(f'  {verb}  {cand.label}  (owner={cand.owner_email})')

        # Every actual API delete call lives behind THIS guard, and only this guard —
        # see test_cleanup_test_data_safety.py's AST scan, which fails a build the
        # moment a delete call appears anywhere it cannot reach.
        if effective_execute and api_available:
            deleted, resource_failed = _delete_candidates(api, resource, candidates)
            if resource_failed:
                exit_code = 1
            report['resources'][resource]['deleted'] = deleted
            print(f'  Deleted {deleted}/{len(candidates)}')

    api.logout()

    # User/LLM-config plane: delegate to cleanup-test-users.py (one implementation, no
    # second copy of ORPHAN_PATTERNS logic). Run LAST — media/collection/tag/watch/
    # profile/conversation deletion above clears the `files > 0` block that otherwise
    # keeps a corpus-owning user (e.g. searchqual-*) permanently un-sweepable.
    subprocess_flag = '--execute-unambiguous' if args.execute_unambiguous else None
    if args.execute:
        subprocess_flag = '--execute'

    cmd = [sys.executable, str(_CLEANUP_USERS_SCRIPT)]
    if subprocess_flag:
        cmd.append(subprocess_flag)
    print(f'\n--- {" ".join(cmd)} ---')
    result = subprocess.run(cmd, capture_output=False, check=False)  # noqa: S603
    if result.returncode not in (0,):
        exit_code = 1

    if args.json:
        print('\n--- JSON report ---')
        print(json_module.dumps(report, indent=2))

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
