"""The store planes (OpenSearch, MinIO, databases) and orphan profile documents."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import requests
from sqlalchemy import create_engine, text

from cleanup_test_data.cutoff import _TESTRUNS_DIR, prune_stale_markers
from cleanup_test_data.settings import _engine_url, _load_artifact_sweep, _resolve_port, _setting

#: Every plane this script sweeps. ``records`` is the original issue #629 sweep (rows, over
#: the API, plus the cleanup-test-users.py subprocess); the other five are the store-level
#: artifacts of issue #1195, implemented in ``scripts/lib/test_artifact_sweep.py``.
PLANES = ('records', 'indexes', 'databases', 'buckets', 'tmp', 'docker')


STORE_PLANES = ('indexes', 'databases', 'buckets')


_MAX_LINES_PER_PLANE = 25


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
        except Exception as exc:  # one bad artifact must not stop the plane
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
    except Exception as exc:  # a missing client library degrades, never crashes
        print(f'  WARN  could not set up the store planes: {exc}')
        return 1

    for name, plane in planes.items():
        try:
            findings = plane.list_findings(cutoff)
        except Exception as exc:  # an unreachable store degrades, never crashes
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
    except Exception as exc:  # a report must not fail the sweep
        print(f'\nspeaker profile docs: UNAVAILABLE ({exc})')
        return
    orphans = find_orphan_profile_docs(docs, live)
    print(f'\nspeaker profile docs (report only): {len(orphans)} with no speaker_profile row')
    for ref in orphans[:_MAX_LINES_PER_PLANE]:
        print(f'  ORPHAN  {ref}  (reconcile from Admin > Data integrity)')
    report['resources']['orphan_profile_docs'] = orphans
