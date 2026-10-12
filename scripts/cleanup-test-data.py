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

Store planes (issue #1195, ``scripts/lib/test_artifact_sweep.py``): besides ROWS, a killed
run leaves artifacts no API reaches — throwaway OpenSearch indexes and ingest pipelines,
MinIO buckets and test-keyed objects, scratch Postgres databases, ``/tmp`` trees, and labelled
throwaway docker containers/networks. Same tiers, same liveness cutoff, plus: a name must FULLY
match a registered test shape, protected names (production indexes, configured buckets, the
target database, ``.``-prefixed system indexes) are refused before any shape is consulted, and
every delete re-classifies its target first. Docker is Tier B only (never automatic). The store
planes refuse to run (exit 2) when the exported ``*_PORT`` values point at different stacks.

Usage (from repo root, inside backend venv):
    python scripts/cleanup-test-data.py                        # dry run, all planes, all tiers
    python scripts/cleanup-test-data.py --execute-unambiguous   # delete Tier A only
    python scripts/cleanup-test-data.py --execute               # delete Tier A + Tier B
    python scripts/cleanup-test-data.py --planes indexes,tmp    # a subset of planes
    python scripts/cleanup-test-data.py --json                  # machine-readable report

Against an isolated stack, export every port it uses (``POSTGRES_PORT``, ``OPENSEARCH_PORT``,
``MINIO_PORT``, ``E2E_BACKEND_URL``), as ``./opentr.sh --fresh --port-offset N`` sets them.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json as json_module
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import requests
from dotenv import dotenv_values
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLEANUP_USERS_SCRIPT = _REPO_ROOT / 'scripts' / 'cleanup-test-users.py'
_ARTIFACT_SWEEP_MODULE = _REPO_ROOT / 'scripts' / 'lib' / 'test_artifact_sweep.py'


def default_testruns_dir() -> Path:
    """The liveness registry: the MAIN checkout's ``.testruns`` (shared by every worktree).

    Same resolution as ``scripts/testrun-registry.sh`` and ``backend/tests/testrun_marker.py``
    (``unit/test_testrun_registry_location.py`` pins the three together). A per-worktree
    directory hid a run in one worktree from a sweep started in another against the same
    stack. ``TESTRUN_REGISTRY_DIR`` overrides it, for tests that need a throwaway registry.
    """
    override = os.environ.get('TESTRUN_REGISTRY_DIR')
    if override:
        return Path(override)
    git = shutil.which('git')
    if git is None:
        return _REPO_ROOT / '.testruns'
    argv = [git, '-C', str(_REPO_ROOT), 'rev-parse', '--path-format=absolute', '--git-common-dir']
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv from a resolved binary, no shell
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return _REPO_ROOT / '.testruns'
    common = result.stdout.strip()
    if result.returncode != 0 or not common:
        return _REPO_ROOT / '.testruns'
    return Path(common).parent / '.testruns'


_TESTRUNS_DIR = default_testruns_dir()

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


def apply_age_floor(cutoff: int, min_age_minutes: int, *, now: int | None = None) -> int:
    """Lower the cutoff so nothing younger than ``min_age_minutes`` is ever a candidate.

    The liveness markers protect every REGISTERED run; the floor protects the rest — an ad
    hoc script, a browser session, a test started from a checkout whose registry this sweep
    cannot see. ``scripts/cleanup-test-leftovers.sh`` runs unattended, so it always sets one.
    """
    if min_age_minutes <= 0:
        return cutoff
    resolved_now = now if now is not None else int(time.time())
    return min(cutoff, resolved_now - min_age_minutes * 60)


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
    # integration/test_lite_mode_mocked_providers.py's per-test upload. It had no
    # backstop here at all, so an interrupted run leaked one file per test.
    MediaFilenameSpec(
        'lite-mode', r'lite-mode-test-%', re.compile(r'^lite-mode-test-[0-9a-f]{8}\.wav$')
    ),
    # integration/test_deletion_residue_live.py + test_orphan_reconcile_opensearch.py
    # (deletion_seed.DELRES_PREFIX): synthetic fully-processed files seeded straight into
    # every store. Teardown removes them; this is the backstop for a killed run.
    MediaFilenameSpec(
        'deletion-residue', r'e2e-delres-%', re.compile(r'^e2e-delres-[0-9a-f]{8}\.wav$')
    ),
    # e2e/test_full_pipeline_smoke.py's real upload (issue #1195): one unchecked DELETE was
    # its only cleanup, so a failed teardown left a fully processed file behind.
    MediaFilenameSpec(
        'pipeline-smoke', r'pipeline-smoke-%', re.compile(r'^pipeline-smoke-[0-9a-f]{8}\.wav$')
    ),
    # integration/test_diar_native_{cross_card_placement,multigpu_provider}_live.py's uploads,
    # uniquified by pid: diar-native-crosscard-<pid>.wav, diar-native-<topology>-<pid>.wav.
    MediaFilenameSpec(
        'diar-native-live',
        r'diar-native-%',
        re.compile(r'^diar-native-[a-z0-9]+(-[a-z0-9]+)*-\d+\.wav$'),
    ),
    # scripts/benchmark_parallel.py --upload (issue #1222): standalone runs used to upload the
    # corpus's REAL filenames, which no sweep can tell from a user's own recordings.
    MediaFilenameSpec(
        'bench-parallel',
        r'bench-parallel-%',
        re.compile(r'^bench-parallel-[0-9a-f]{8}\.[A-Za-z0-9]+$'),
    ),
]

#: Namespace seeds the TEST suites pass to ``corpus_injection.injector.inject_meeting``. The
#: injected file's name (``<corpus>__<meeting>.transcript``) is the corpus's, not ours, so these
#: files are recognised by the ``rag_eval.seed`` the injector stamps on them instead. FULL
#: match with the uuid4 uniquifier: the canonical (empty) seed and any operator-chosen seed
#: (``run1``, ``rag403``) are real eval corpora and are never a candidate. Producers:
#: ``integration/test_corpus_injection{,_synthetic_e2e,_purge}.py`` (``pytest-<hex12>``,
#: ``pytest-synth-<hex10>``), ``fixtures/search_corpus_stack.py`` (``pytest-searchqual-<hex10>``)
#: and ``e2e/owned_corpus.py`` (``e2e-owned-<hex10>``).
INJECTED_TEST_SEED_SHAPE = re.compile(r'(pytest(-synth|-searchqual)?|e2e-owned)-[0-9a-f]{10,12}')

#: name/title-prefix + <8hex> shape, one entry per non-media resource type.
NAME_PREFIX_SPECS: dict[str, tuple[str, str]] = {
    # label: (table, column)
    'collection': ('collection', 'name'),
    'tag': ('tag', 'name'),
    'watch_source': ('watch_source', 'name'),
    'speaker_profile': ('speaker_profile', 'name'),
    'chat_conversation': ('chat_conversation', 'title'),
    # Clusters a test's speakers form (deletion_seed's `e2e-delres-<hex8>[-shared]`, a profile-
    # backed cluster labelled after an e2e profile). A profile delete only NULLs the cluster's
    # promoted_to_profile_id, and an empty promoted cluster is not reaped, so these outlive the
    # test that made them (issue #1195). Deleted through the API: it also drops the centroid.
    'speaker_cluster': ('speaker_cluster', 'label'),
}

NAME_PREFIXES: dict[str, list[str]] = {
    'collection': ['e2e-shared-', 'e2e-collection-', 'e2e-delres-'],
    'tag': ['e2e-tag-', 'e2e-delres-'],
    'watch_source': ['e2e-watch-', 'e2e-delres-'],
    'speaker_profile': ['e2e-gender-', 'e2e-delres-', 'e2e-propagated-'],
    # probe-: scripts/probe_chat_rag.py's per-run 'probe-<hex8>: <label>' titles (issue #1195).
    'chat_conversation': ['e2e-chat-', 'e2e-delres-', 'probe-'],
    'speaker_cluster': ['e2e-gender-', 'e2e-propagated-', 'e2e-delres-'],
}

_HEX8_SHAPE = re.compile(r'[0-9a-f]{8}')

#: Names tests minted BEFORE they were renamed to a registered lowercase prefix. Kept so a
#: leftover already sitting on a stack is still swept; nothing may mint these any more
#: (test_cleanup_test_data_safety.py's e2e literal gate refuses an unregistered head). FULL
#: match, uniquifier included, so a human's "E2E Propagated notes" is never one.
LEGACY_NAME_SHAPES: dict[str, list[re.Pattern[str]]] = {
    # test_speaker_gender_clusters.py's rename target until 2026-10-11 (issue #1195): the
    # rename creates a speaker profile the test never deleted. Seen on the v060 stack.
    'speaker_profile': [re.compile(r'E2E Propagated [0-9a-f]{8}')],
}


def _name_matches_shape(name: str, prefixes: list[str], resource: str | None = None) -> bool:
    """A registered prefix followed somewhere by an 8-hex-char uniquifier — never a
    bare prefix match, so a human-created ``e2e-tag-notes`` (no hex suffix) survives.
    ``resource`` additionally admits that resource's FULL-match legacy shapes.
    """
    for prefix in prefixes:
        if name.startswith(prefix) and _HEX8_SHAPE.search(name[len(prefix) :]):
            return True
    return any(shape.fullmatch(name) for shape in LEGACY_NAME_SHAPES.get(resource or '', []))


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


def find_injected_test_media(conn: Any, cutoff: int) -> list[Candidate]:
    """Files ``app.scripts.corpus_injection`` wrote under a registered TEST seed.

    Deleted through ``DELETE /api/files/{uuid}`` like every other media candidate, which ends
    in ``purge_media_file`` -- the same destroyer ``python -m app.scripts.corpus_injection
    --purge`` uses -- so the OpenSearch chunks and speaker rows go with the file.
    """
    rows = conn.execute(
        text(
            """
            SELECT m.uuid, m.filename, u.email,
                   m.metadata_important->'rag_eval'->>'seed' AS seed
            FROM media_file m
            JOIN "user" u ON u.id = m.user_id
            WHERE m.metadata_important->'rag_eval'->>'injected' = 'true'
              AND m.upload_time < to_timestamp(:cutoff)
            """
        ),
        {'cutoff': cutoff},
    ).fetchall()
    return [
        Candidate('media_file', str(row.uuid), f'{row.filename} [seed {row.seed}]', row.email)
        for row in rows
        if row.seed and INJECTED_TEST_SEED_SHAPE.fullmatch(row.seed)
    ]


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
            if row.label and _name_matches_shape(row.label, NAME_PREFIXES[resource], resource)
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
    'speaker_cluster': '/api/speaker-clusters/{uuid}',
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

    def delete_user(self, user_uuid: str, email: str) -> bool:
        """``DELETE /api/admin/users/{uuid}``: the account and everything it owns (cascade +
        storage purge). The only way to remove a row the admin is not authorised on."""
        assert self.session is not None
        url = f'{self.backend_url}/api/admin/users/{user_uuid}'
        try:
            resp = self.session.delete(url, timeout=120)
        except requests.RequestException as exc:
            print(f'  FAILED  DELETE user {email} ({user_uuid}): {exc}')
            return False
        if resp.status_code in (200, 204, 404):
            return True
        print(f'  FAILED  DELETE user {email} ({user_uuid}): {describe_failure(resp)}')
        return False

    def delete_media_file(self, file_uuid: str) -> bool:
        """Mirrors ``e2e/conftest.py``'s ``delete_media_file``: wait out an
        ``active_task_id``, DELETE, fall back to ``/force`` on persistent 409."""
        assert self.session is not None
        last = 'no response'
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
                last = describe_failure(resp)
            except requests.RequestException as exc:
                last = str(exc)
            time.sleep(5)
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/files/{file_uuid}/force', timeout=30
            )
            if resp.status_code in (200, 204, 404):
                return True
            last = describe_failure(resp)
        except requests.RequestException as exc:
            last = str(exc)
        print(f'  FAILED  DELETE media_file {file_uuid}: {last}')
        return False

    def delete_simple(self, resource: str, resource_uuid: str) -> bool:
        assert self.session is not None
        path_template = _DELETE_PATH_BY_RESOURCE[resource]
        assert path_template is not None, f'{resource} has no simple delete path'
        url = f'{self.backend_url}{path_template.format(uuid=resource_uuid)}'
        try:
            resp = self.session.delete(url, timeout=30)
        except requests.RequestException as exc:
            print(f'  FAILED  DELETE {resource} {resource_uuid}: {exc}')
            return False
        if resp.status_code in (200, 204, 404):
            return True
        print(f'  FAILED  DELETE {resource} {resource_uuid}: {describe_failure(resp)}')
        return False

    def delete_tags(self, tag_uuids: list[str]) -> bool:
        assert self.session is not None
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/tags',
                params=[('tag_uuids', u) for u in tag_uuids],
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f'  FAILED  DELETE {len(tag_uuids)} tag(s): {exc}')
            return False
        if not resp.ok:
            print(f'  FAILED  DELETE {len(tag_uuids)} tag(s): {describe_failure(resp)}')
        return bool(resp.ok)


def describe_failure(resp: Any) -> str:
    """``HTTP <status> <reason>: <detail>`` for a failed delete, bounded in length.

    The backend's ``detail`` is the reason an operator needs ("Not authorized to access this
    profile"); a non-JSON body falls back to its first 200 characters.
    """
    reason = getattr(resp, 'reason', '') or ''
    head = f'HTTP {resp.status_code}' + (f' {reason}' if reason else '')
    try:
        body = resp.json()
        detail = body.get('detail') if isinstance(body, dict) else None
    except ValueError:
        detail = None
    if detail is None:
        detail = (getattr(resp, 'text', '') or '').strip()[:200]
    return f'{head}: {str(detail)[:200]}' if detail else head


# ---------------------------------------------------------------------------------------------
# Owner deletion (issue #1237)
# ---------------------------------------------------------------------------------------------


def owner_patterns(users_module: ModuleType, *, include_tier_b: bool) -> list[str]:
    """Registered test-user email patterns whose accounts may be deleted: Tier A always,
    the review tier only under the full ``--execute``."""
    return list(
        users_module.ORPHAN_PATTERNS if include_tier_b else users_module.ORPHAN_PATTERNS_UNAMBIGUOUS
    )


def find_deletable_test_owners(conn: Any, cutoff: int, patterns: list[str]) -> dict[str, str]:
    """``{email: uuid}`` of registered test users created before the liveness cutoff.

    The admin is not authorised on another user's rows (a speaker profile is a 403), so the
    only delete that reaches them is the owner's own, which cascades. An account is only a
    candidate here when its email FULLY matches a registered test shape, it predates every
    live run, and it is not one of ``cleanup-test-users.py``'s keep-list accounts.
    """
    if not patterns:
        return {}
    users_module = _load_cleanup_users_module()
    where = ' OR '.join(f"u.email LIKE '{p}' ESCAPE '\\'" for p in patterns)
    rows = conn.execute(
        text(
            f"""
            SELECT u.uuid, u.email
            FROM "user" u
            WHERE ({where})
              AND u.created_at < to_timestamp(:cutoff)
            """
        ),
        {'cutoff': cutoff},
    ).fetchall()
    return {
        row.email: str(row.uuid)
        for row in rows
        if row.email not in users_module.KEEP_EMAILS
        and not row.email.startswith(users_module.KEEP_PREFIXES)
    }


def owners_to_delete(
    candidates_by_resource: dict[str, list[Candidate]], deletable: dict[str, str]
) -> dict[str, str]:
    """The ``{email: uuid}`` owners that at least one candidate row belongs to."""
    return {
        cand.owner_email: deletable[cand.owner_email]
        for candidates in candidates_by_resource.values()
        for cand in candidates
        if cand.owner_email in deletable
    }


# ---------------------------------------------------------------------------------------------
# Reporting / main
# ---------------------------------------------------------------------------------------------


def _delete_owners(api: ApiSession, owners: dict[str, str]) -> dict[str, bool]:
    """Delete registered test-user accounts (cascading their rows). ``{email: succeeded}``.

    Like ``_delete_candidates``, reached only through the ``effective_execute`` guard.
    """
    outcome: dict[str, bool] = {}
    for email, user_uuid in owners.items():
        outcome[email] = api.delete_user(user_uuid, email)
    return outcome


def _delete_candidates(
    api: ApiSession,
    resource: str,
    candidates: list[Candidate],
    owner_outcome: dict[str, bool],
) -> tuple[int, bool]:
    """Issue the real deletes for one resource type. Returns ``(deleted_count, any_failed)``.

    A candidate whose owner was handled by ``_delete_owners`` is not deleted individually: the
    owner delete removed it (counted deleted) or failed (counted failed, already reported).

    The ONLY place in this module that calls ``api.delete_media_file`` /
    ``api.delete_simple`` / ``api.delete_tags`` — callers must reach this function
    through the ``if effective_execute and api_available:`` guard in ``main()``.
    """
    deleted = 0
    any_failed = False
    for cand in candidates:
        if cand.owner_email in owner_outcome:
            if owner_outcome[cand.owner_email]:
                deleted += 1
            else:
                any_failed = True
    candidates = [c for c in candidates if c.owner_email not in owner_outcome]
    if resource == 'tag':
        if candidates and api.delete_tags([c.uuid for c in candidates]):
            deleted += len(candidates)
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


#: Every plane this script sweeps. ``records`` is the original issue #629 sweep (rows, over
#: the API, plus the cleanup-test-users.py subprocess); the other five are the store-level
#: artifacts of issue #1195, implemented in ``scripts/lib/test_artifact_sweep.py``.
PLANES = ('records', 'indexes', 'databases', 'buckets', 'tmp', 'docker')
STORE_PLANES = ('indexes', 'databases', 'buckets')
_MAX_LINES_PER_PLANE = 25


def _parse_planes(value: str) -> tuple[str, ...]:
    planes = tuple(p.strip() for p in value.split(',') if p.strip())
    unknown = [p for p in planes if p not in PLANES]
    if unknown or not planes:
        raise argparse.ArgumentTypeError(
            f'unknown plane(s) {unknown or value!r}; choose from {",".join(PLANES)}'
        )
    return planes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--execute-unambiguous', action='store_true', help='Delete Tier A candidates only'
    )
    parser.add_argument(
        '--execute', action='store_true', help='Delete Tier A + Tier B (review) candidates'
    )
    parser.add_argument('--json', action='store_true', help='Machine-readable aggregate report')
    parser.add_argument(
        '--min-age-minutes',
        type=int,
        default=0,
        help='Never touch anything younger than this (on top of the liveness cutoff)',
    )
    parser.add_argument(
        '--planes',
        type=_parse_planes,
        default=PLANES,
        help=f'Comma-separated subset of {",".join(PLANES)} (default: all)',
    )
    return parser


def _load_artifact_sweep() -> ModuleType:
    """``scripts/lib/test_artifact_sweep.py`` by path (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location('test_artifact_sweep', _ARTIFACT_SWEEP_MODULE)
    assert spec is not None and spec.loader is not None, f'cannot load {_ARTIFACT_SWEEP_MODULE}'
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _resolve_port(sweep: ModuleType, var: str) -> Any:
    """Env first, then ``.env``, then the stack default: the order ``_setting`` uses."""
    if os.environ.get(var):
        return sweep.ResolvedPort(var, int(os.environ[var]), 'env')
    if _env.get(var):
        return sweep.ResolvedPort(var, int(str(_env[var])), 'dotenv')
    return sweep.ResolvedPort(var, sweep.BASE_PORTS[var], 'default')


def prune_stale_markers(testruns_dir: Path, *, execute: bool, grace_seconds: int) -> list[Path]:
    """Marker files no process holds any more (their run exited or was killed).

    A marker younger than ``grace_seconds`` is never pruned: ``testrun_begin`` creates the
    file a moment BEFORE it takes the lock, and deleting it inside that window would leave
    the run holding a lock on an unlinked file, invisible to every later sweep.
    """
    if not testruns_dir.is_dir():
        return []
    stale: list[Path] = []
    for marker in sorted(testruns_dir.glob('*.lock')):
        try:
            if time.time() - marker.stat().st_mtime < grace_seconds:
                continue
            fd = os.open(str(marker), os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue  # held: a live run
        else:
            stale.append(marker)
            if execute:
                with contextlib.suppress(OSError):
                    marker.unlink()
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return stale


def _delete_artifacts(
    plane: Any, findings: list[Any], cutoff: int, *, include_tier_b: bool
) -> tuple[int, bool]:
    """The ONLY caller of a store plane's ``delete``. Each delete re-classifies its target
    first, so a protected or no-longer-eligible name is refused at the point of deletion too.
    """
    deleted = 0
    any_failed = False
    for finding in findings:
        try:
            if plane.delete_one(finding, cutoff, include_tier_b=include_tier_b):
                deleted += 1
            else:
                any_failed = True
                print(f'  REFUSED/FAILED  {finding.name} (re-check did not allow it)')
        except Exception as exc:  # noqa: BLE001 - one bad artifact must not stop the plane
            any_failed = True
            print(f'  FAILED  {finding.name}: {exc}')
    return deleted, any_failed


def _build_store_planes(sweep: ModuleType, planes: tuple[str, ...]) -> dict[str, Any]:
    built: dict[str, Any] = {}
    if 'indexes' in planes:
        os_port = _resolve_port(sweep, 'OPENSEARCH_PORT').port
        os_host = os.environ.get('OPENSEARCH_HOST', 'localhost')
        scheme = 'https' if _setting('OPENSEARCH_USE_TLS', 'false').lower() == 'true' else 'http'
        session = requests.Session()
        user = _setting('OPENSEARCH_USER', '')
        if user:
            session.auth = (user, _setting('OPENSEARCH_PASSWORD', ''))
        session.verify = _setting('OPENSEARCH_VERIFY_CERTS', 'false').lower() == 'true'
        built['indexes'] = sweep.OpenSearchPlane(session, f'{scheme}://{os_host}:{os_port}')
    if 'databases' in planes:
        built['databases'] = sweep.PostgresDatabasePlane(
            create_engine(_engine_url()), _setting('POSTGRES_DB', 'opentranscribe')
        )
    if 'buckets' in planes:
        from minio import Minio

        minio_port = _resolve_port(sweep, 'MINIO_PORT').port
        client = Minio(
            f'{os.environ.get("MINIO_HOST", "localhost")}:{minio_port}',
            access_key=_setting('MINIO_ROOT_USER', 'minioadmin'),
            secret_key=_setting('MINIO_ROOT_PASSWORD', 'minioadmin'),
            secure=_setting('MINIO_SECURE', 'false').lower() == 'true',
        )
        configured = {
            'MEDIA_BUCKET_NAME': _setting('MEDIA_BUCKET_NAME', 'opentranscribe'),
            'CACHE_BUCKET_NAME': _setting('CACHE_BUCKET_NAME', 'processed-videos'),
        }
        built['buckets'] = sweep.MinioPlane(client, set(configured.values()), configured)
    if 'tmp' in planes:
        built['tmp'] = sweep.TmpPlane(Path(tempfile.gettempdir()))
    if 'docker' in planes:
        docker = shutil.which('docker')
        if docker is not None:
            built['docker'] = sweep.DockerPlane(docker)
        else:
            print('\ndocker: docker CLI not found, plane skipped')
    return built


def _sweep_store_planes(args: argparse.Namespace, cutoff: int, report: dict[str, Any]) -> int:
    """Indexes, databases, buckets and /tmp trees left by killed test runs (issue #1195)."""
    sweep = _load_artifact_sweep()
    effective_execute = args.execute or args.execute_unambiguous
    include_tier_b = bool(args.execute)
    exit_code = 0

    # Only the stores this run will actually open are compared: a databases-only pass
    # touches Postgres alone and has nothing to disagree with.
    port_vars = {'POSTGRES_PORT'}
    if 'indexes' in args.planes:
        port_vars.add('OPENSEARCH_PORT')
    if 'buckets' in args.planes:
        port_vars.add('MINIO_PORT')
    store_planes = [p for p in args.planes if p in STORE_PLANES]
    if store_planes:
        ports = [
            _resolve_port(sweep, v)
            for v in ('POSTGRES_PORT', 'OPENSEARCH_PORT', 'MINIO_PORT')
            if v in port_vars
        ]
        mismatch = sweep.port_scope_mismatch(ports)
        print('\nStores: ' + ', '.join(f'{p.var}={p.port} ({p.source})' for p in ports))
        if mismatch:
            print(f'  REFUSED  {mismatch}')
            report['store_planes_refused'] = mismatch
            return 2

    try:
        planes = _build_store_planes(sweep, args.planes)
    except Exception as exc:  # noqa: BLE001 - a missing client library degrades, never crashes
        print(f'  WARN  could not set up the store planes: {exc}')
        return 1

    for name, plane in planes.items():
        try:
            findings = plane.list_findings(cutoff)
        except Exception as exc:  # noqa: BLE001 - an unreachable store degrades, never crashes
            print(f'\n{name}: UNAVAILABLE ({exc})')
            report['resources'][name] = {'error': str(exc)}
            exit_code = 1
            continue
        tier_a = [f for f in findings if f.tier == sweep.TIER_A]
        tier_b = [f for f in findings if f.tier == sweep.TIER_B]
        # Registered shape, but held back (may be live, has connections, no test label).
        held = [
            f
            for f in findings
            if f.tier is None and 'registered' not in f.reason and 'protected' not in f.reason
        ]
        protected = [f for f in findings if 'protected' in f.reason]
        print(f'\n{name}: {len(tier_a)} Tier A, {len(tier_b)} Tier B candidate(s)')
        for i, f in enumerate(tier_a + tier_b):
            if i == _MAX_LINES_PER_PLANE:
                print(f'  ... and {len(tier_a) + len(tier_b) - i} more (--json lists all)')
                break
            chosen = sweep.selected(f, include_tier_b=include_tier_b)
            verb = 'DELETE' if (effective_execute and chosen) else 'WOULD DELETE'
            if not chosen:
                verb = 'REVIEW (--execute)'
            print(f'  {verb}  [{f.tier}] {f.name}  ({f.reason})')
        for f in held[:_MAX_LINES_PER_PLANE]:
            print(f'  KEEP  {f.name}  ({f.reason})')
        if protected:
            print(f'  KEEP  {len(protected)} protected name(s), never swept')
        report['resources'][name] = {
            'tier_a': [f.name for f in tier_a],
            'tier_b': [f.name for f in tier_b],
            'deleted': 0,
        }
        targets = [f for f in tier_a + tier_b if sweep.selected(f, include_tier_b=include_tier_b)]
        # Every store delete is behind THIS guard — test_cleanup_test_data_safety.py's AST
        # scan fails the build on a `_delete_artifacts(...)` call it cannot see inside one.
        if effective_execute and targets:
            deleted, failed = _delete_artifacts(
                plane, targets, cutoff, include_tier_b=include_tier_b
            )
            report['resources'][name]['deleted'] = deleted
            print(f'  Deleted {deleted}/{len(targets)}')
            if failed:
                exit_code = 1

    if 'indexes' in planes:
        _report_orphan_profile_docs(planes['indexes'], report)

    if 'tmp' in args.planes:
        stale = prune_stale_markers(
            _TESTRUNS_DIR, execute=effective_execute, grace_seconds=sweep.GRACE_SECONDS
        )
        verb = 'Removed' if effective_execute else 'Would remove'
        print(f'\n.testruns: {verb} {len(stale)} stale liveness marker(s) in {_TESTRUNS_DIR}')
    return exit_code


def find_orphan_profile_docs(docs: list[dict[str, Any]], live_uuids: set[str]) -> list[str]:
    """Speaker-index profile docs (``profile_<uuid>``) whose ``speaker_profile`` row is gone.

    Pure. A profile deleted through the app takes its doc with it; one deleted any other way
    (raw SQL, a killed request) leaves a voiceprint that still matches new speakers.
    """
    orphans = []
    for doc in docs:
        source = doc.get('_source') or {}
        profile_uuid = str(source.get('profile_uuid') or '')
        if profile_uuid and profile_uuid not in live_uuids:
            orphans.append(f'{doc.get("_index")}/{doc.get("_id")}')
    return sorted(orphans)


def _report_orphan_profile_docs(plane: Any, report: dict[str, Any]) -> None:
    """REPORT ONLY (Tier C): the speaker index is a production index this tool never writes.
    The fix for an orphan is the data-integrity reconcile in the admin UI, not this script."""
    try:
        resp = plane.session.post(
            f'{plane.base_url}/speakers*/_search',
            json={
                'size': 10000,
                'query': {'exists': {'field': 'profile_uuid'}},
                '_source': ['profile_uuid', 'profile_name'],
            },
            timeout=30,
        )
        resp.raise_for_status()
        docs = resp.json().get('hits', {}).get('hits', [])
        with create_engine(_engine_url()).connect() as conn:
            live = {str(r[0]) for r in conn.execute(text('SELECT uuid FROM speaker_profile'))}
    except Exception as exc:  # noqa: BLE001 - a report must not fail the sweep
        print(f'\nspeaker profile docs: UNAVAILABLE ({exc})')
        return
    orphans = find_orphan_profile_docs(docs, live)
    print(f'\nspeaker profile docs (report only): {len(orphans)} with no speaker_profile row')
    for ref in orphans[:_MAX_LINES_PER_PLANE]:
        print(f'  ORPHAN  {ref}  (reconcile from Admin > Data integrity)')
    report['resources']['orphan_profile_docs'] = orphans


def _sweep_records(args: argparse.Namespace, cutoff: int, report: dict[str, Any]) -> int:
    effective_execute = args.execute or args.execute_unambiguous
    backend_url = _backend_url()
    cleanup_users = _load_cleanup_users_module()
    engine = create_engine(_engine_url())
    exit_code = 0
    with engine.connect() as conn:
        media_candidates = find_media_by_filename(conn, cutoff)
        media_candidates += find_injected_test_media(conn, cutoff)
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
        deletable_owners = find_deletable_test_owners(
            conn, cutoff, owner_patterns(cleanup_users, include_tier_b=bool(args.execute))
        )

    all_candidates: dict[str, list[Candidate]] = {'media_file': media_candidates, **named}
    owners = owners_to_delete(all_candidates, deletable_owners)

    api = ApiSession(backend_url)
    api_available = api.login()
    if not api_available:
        print('  WARN  API plane unavailable — skipping media/collection/tag/watch-source/')
        print('        speaker-profile/conversation deletion this run (DB-only planes still run)')
        exit_code = 1

    owner_outcome: dict[str, bool] = {}
    print(f'\nregistered test owners: {len(owners)} account(s) to delete with their rows')
    for email in owners:
        verb = 'DELETE' if (effective_execute and api_available) else 'WOULD DELETE'
        print(f'  {verb}  user {email}  (rows the admin cannot delete individually)')
    report['resources']['test_owners'] = {'count': len(owners), 'deleted': 0}
    # Same guard as the per-record deletes below (test_cleanup_test_data_safety.py's AST scan).
    if effective_execute and api_available and owners:
        owner_outcome = _delete_owners(api, owners)
        report['resources']['test_owners']['deleted'] = sum(owner_outcome.values())
        if not all(owner_outcome.values()):
            exit_code = 1
        print(f'  Deleted {sum(owner_outcome.values())}/{len(owners)} owner account(s)')

    for resource, candidates in all_candidates.items():
        print(f'\n{resource}: {len(candidates)} Tier A candidate(s)')
        report['resources'][resource] = {'count': len(candidates), 'deleted': 0}
        for cand in candidates:
            verb = 'DELETE' if (effective_execute and api_available) else 'WOULD DELETE'
            via = ' via owner' if cand.owner_email in owners else ''
            print(f'  {verb}{via}  {cand.label}  (owner={cand.owner_email})')

        # Every actual API delete call lives behind THIS guard, and only this guard —
        # see test_cleanup_test_data_safety.py's AST scan, which fails a build the
        # moment a delete call appears anywhere it cannot reach.
        if effective_execute and api_available:
            deleted, resource_failed = _delete_candidates(api, resource, candidates, owner_outcome)
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
    print(f'\n--- {" ".join(cmd)} ---', flush=True)
    result = subprocess.run(cmd, capture_output=False, check=False)  # noqa: S603
    if result.returncode not in (0,):
        exit_code = 1

    return exit_code


def main() -> int:
    args = build_parser().parse_args()

    backend_url = _backend_url()
    db_user = _setting('POSTGRES_USER', 'postgres')
    db_host = _host_setting()
    db_port = _setting('POSTGRES_PORT', '5176')
    db_name = _setting('POSTGRES_DB', 'opentranscribe')
    print(f'Target: {db_user}@{db_host}:{db_port}/{db_name} + API {backend_url}')
    print(f'Planes: {",".join(args.planes)}')

    live_starts = live_marker_start_times(_TESTRUNS_DIR)
    cutoff = apply_age_floor(resolve_cutoff(live_starts), args.min_age_minutes)
    print(
        f'Cutoff: rows created before {cutoff} ({len(live_starts)} concurrently live '
        f'run marker(s) found in {_TESTRUNS_DIR}; age floor {args.min_age_minutes} min)'
    )

    report: dict[str, Any] = {'cutoff': cutoff, 'planes': list(args.planes), 'resources': {}}
    exit_code = 0
    if 'records' in args.planes:
        exit_code = max(exit_code, _sweep_records(args, cutoff, report))
    if any(p != 'records' for p in args.planes):
        exit_code = max(exit_code, _sweep_store_planes(args, cutoff, report))

    if args.json:
        print('\n--- JSON report ---')
        print(json_module.dumps(report, indent=2))

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
