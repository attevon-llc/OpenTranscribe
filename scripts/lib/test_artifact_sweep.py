"""Store-level planes of ``scripts/cleanup-test-data.py`` (issue #1195).

``cleanup-test-data.py`` sweeps the ROWS a killed test run leaves behind (media, tags,
collections, ...), and deletes them through the HTTP API so each resource's own cascade runs.
This module sweeps the artifacts that are not rows at all and that no API can reach: the
throwaway OpenSearch indexes, MinIO buckets, Postgres databases and ``/tmp`` trees that test
fixtures create beside the stack. Each fixture removes its own in a ``finally``; what reaches
this module is what a SIGKILLed xdist worker or an aborted run never got to remove (120 empty
``test_summary_api_<hex>`` indexes on one isolated stack, 7,358 ``opentranscribe_test_*``
directories in ``/tmp``).

Safety model, in the order it is applied to every name:

1. **Refuse first.** A name on the protected list (the production indexes, the media bucket,
   the target database, ``postgres``/``template*``, anything starting ``.``) is refused even if
   a registered shape would match it. The check runs again immediately before each delete.
2. **Registered shape or nothing.** Only a FULL match against a registered shape is a
   candidate, and every shape here ends in the random uniquifier its fixture generates, so a
   human-made ``test_summary_api_notes`` index is not one. There is no "looks like a test"
   heuristic: an unregistered leftover is reported by nobody, and the fix is to register it
   (``unit/test_test_artifact_sweep.py`` fails when a fixture mints an unregistered name).
3. **Not possibly live.** Anything created at or after the liveness cutoff (the start of the
   oldest live test run, see ``scripts/testrun-registry.sh``) minus a grace window is left
   alone; a database with any open connection is left alone.
4. **Tiers.** Tier A (``--execute-unambiguous``) is a registered shape whose content cannot
   matter: an EMPTY index, a uniquified test database or bucket, a test tmp tree. Tier B
   (``--execute``) is a registered shape that still holds documents, or a hand-made scratch
   database (``ot_<word>_test``) a person may still want.

Every function that decides is pure and unit-tested; the I/O wrappers re-run the decision
immediately before deleting, so a decision cannot be bypassed by calling a delete directly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

#: Seconds subtracted from the liveness cutoff. The cutoff already protects every registered
#: live run; this covers the window between a fixture creating a store and a run that never
#: registered (an ad-hoc script) using it, and clock rounding between host and container.
GRACE_SECONDS = 120

TIER_A = 'A'
TIER_B = 'B'


class ArtifactSpec(NamedTuple):
    label: str
    shape: re.Pattern[str]  # FULL match required
    tier: str  # tier when the artifact is otherwise sweepable (an index can drop to B if non-empty)
    origin: str  # where the name is minted, so a reader can check the shape against it


def _spec(label: str, pattern: str, tier: str, origin: str) -> ArtifactSpec:
    return ArtifactSpec(label, re.compile(pattern), tier, origin)


# ---------------------------------------------------------------------------------------------
# Registries. Every shape is anchored and ends in the fixture's own random uniquifier.
# ---------------------------------------------------------------------------------------------

_H12 = '[0-9a-f]{12}'  # uuid4().hex[:12]
_H10 = '[0-9a-f]{10}'  # uuid4().hex[:10]
_H8 = '[0-9a-f]{8}'
_UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_MKDTEMP = '[a-z0-9_]{8}'  # tempfile's random suffix alphabet and length

#: Every throwaway index (and ingest pipeline: same namespace rules) a test creates on the
#: live cluster. An EMPTY one is Tier A; one still holding documents is Tier B.
TEST_INDEX_SPECS: list[ArtifactSpec] = [
    _spec('summary-api', rf'test_summary_api_{_H12}', TIER_A, 'fixtures/summary_plane.py'),
    _spec(
        'transcripts-tenancy',
        rf'test_transcripts_(tenancy|pre_tenant|meta)_{_H10}',
        TIER_A,
        'test_transcripts_index_tenancy.py, unit/test_speaker_metadata.py',
    ),
    _spec(
        'chunks-plane',
        rf'test_(search_filters_quarantine|kw_latency|chat_cache_revoke|digest_plane'
        rf'|label_drift|summary_plane|summary_plane_semantic|completeness|chunk_prune'
        rf'|rename_chunks|rename_sites|speaker_id_fields|reindex666_chunks'
        rf'|reindex666_transcripts|org_collection_retrieval|audit_org_scope)_{_H12}',
        TIER_A,
        'integration/test_*_opensearch.py and siblings (one throwaway chunks index each)',
    ),
    _spec(
        'speaker-versioned',
        rf'test_(vp_erase|speakers_meta|speakers_maint|speakers_maint_v3|speakers_write'
        rf'|tenancy_speakers)'
        rf'_{_H10}_(v3|v4|v3_backup)',
        TIER_A,
        'speaker index suites (OPENSEARCH_SPEAKER_INDEX repointed; _v3/_v4/_v3_backup)',
    ),
    _spec(
        'knn-probe',
        rf'knn_probe_live_test(_non_ann)?_{_H10}',
        TIER_A,
        'integration/test_knn_probe_live.py',
    ),
    # delres-absent-*: names a test points settings at so they must NOT exist; registered
    # anyway, so a regression that makes the code under test create one is still swept.
    _spec(
        'deletion-residue',
        rf'delres-(reconcile|negctl|absent-t|absent-s|absent-sp|absent-v4)-{_H12}',
        TIER_A,
        'integration/test_orphan_reconcile_opensearch.py, test_deletion_residue_live.py',
    ),
]

#: Ingest pipelines a test registers (listed with the index plane, same tiering rules
#: except that a pipeline holds no documents and so is always Tier A when it matches).
TEST_PIPELINE_SPECS: list[ArtifactSpec] = [
    _spec(
        'completeness-ingest',
        rf'test-completeness-ingest-{_H8}',
        TIER_A,
        'integration/test_search_completeness_opensearch.py',
    ),
]

#: Postgres databases on the TARGET server. No test creates one there (every test database
#: lives in a throwaway container); this exists for the hand-made scratch databases agents
#: and developers create beside the real one (``ot_perfile_test`` on 2026-10-11). Tier B:
#: a person made it on purpose and may still want it.
TEST_DATABASE_SPECS: list[ArtifactSpec] = [
    _spec('scratch', r'ot_[a-z0-9]+(_[a-z0-9]+)*_test', TIER_B, 'hand-made scratch databases'),
]

#: Whole buckets a test creates (and removes in a finally).
TEST_BUCKET_SPECS: list[ArtifactSpec] = [
    _spec(
        'erasure-journal',
        rf'test-erasure-journal-{_H10}',
        TIER_A,
        'unit/test_erasure_journal_object_storage.py',
    ),
    _spec('watch-e2e', rf'e2e-watch-{_H8}', TIER_A, 'backend/scripts/e2e_watch_sources.py'),
    _spec(
        'orphan-sweep',
        rf'e2e-orphan-sweep-{_H12}',
        TIER_A,
        'integration/test_orphan_sweep_storage.py',
    ),
]


class ObjectSpec(NamedTuple):
    label: str
    bucket_setting: str  # which configured bucket the key lives in
    list_prefix: str  # listing prefix (cheap server-side filter)
    shape: re.Pattern[str]  # FULL key match required
    tier: str
    origin: str


#: Objects a test writes into the REAL media/cache buckets under a test-only key.
TEST_OBJECT_SPECS: list[ObjectSpec] = [
    ObjectSpec(
        'presign-revocation',
        'MEDIA_BUCKET_NAME',
        'tests/presign-revocation-907/',
        re.compile(r'tests/presign-revocation-907/[0-9a-f]{32}(-bystander)?\.txt'),
        TIER_A,
        'integration/test_presign_revocation.py',
    ),
    ObjectSpec(
        'issue695-media',
        'MEDIA_BUCKET_NAME',
        'tests/issue695/',
        re.compile(
            rf'tests/issue695/((bystander-)?{_UUID}/(orig\.bin|thumb\.jpg)|avatars/{_UUID}\.jpg)'
        ),
        TIER_A,
        'api/endpoints/test_user_delete_storage_reclaim.py',
    ),
    ObjectSpec(
        'media-security-thumb',
        'MEDIA_BUCKET_NAME',
        'media/test/thumbs/',
        re.compile(rf'media/test/thumbs/{_UUID}\.webp'),
        TIER_A,
        'test_media_security.py',
    ),
    # A real recording could be called "issue695.mp4", so its derived render is Tier B.
    ObjectSpec(
        'issue695-derived',
        'CACHE_BUCKET_NAME',
        'derived/',
        re.compile(r'derived/\d+_issue695_with_speakers(_rab12cd)?\.mp4'),
        TIER_B,
        'api/endpoints/test_user_delete_storage_reclaim.py',
    ),
]

#: Entries directly under the system temp directory.
TEST_TMP_SPECS: list[ArtifactSpec] = [
    _spec('pytest-process', rf'opentranscribe_test_{_MKDTEMP}', TIER_A, 'tests/conftest.py'),
    _spec(
        'backup-status',
        rf'ot-backup-status-test-{_MKDTEMP}',
        TIER_A,
        'unit/test_admin_status_surfaces_are_opaque.py',
    ),
    _spec(
        'compose-project-stub',
        rf'ot-compose-project-stub-{_MKDTEMP}',
        TIER_A,
        'unit/test_compose_project_container_lookup_sigpipe.py',
    ),
    # Playwright storage state: holds a session cookie, so a leftover is worth removing.
    _spec('e2e-storage-state', rf'ot-e2e-storage-{_MKDTEMP}\.json', TIER_A, 'tests/e2e/*.py'),
    # TemporaryDirectory trees: removed by their context manager, except on SIGKILL.
    _spec(
        'context-managed',
        rf'(faithfulness_judge_|otredcheck_llm_json_repair_){_MKDTEMP}',
        TIER_A,
        'tests/eval/harness/faithfulness_judge.py, unit/test_llm_summary_json_repair.py',
    ),
    # Release-harness scratch: each is removed by its own script on a normal exit.
    _spec(
        'release-scratch',
        r'(ot-release-smoke-probe|ot-selftest-rollback|ot-reltest-hop-cleanup)\.[A-Za-z0-9]{6}',
        TIER_A,
        'release/85-smoke.sh, release-tests/selftest-rollback-fault-injection.sh, test-upgrade.sh',
    ),
    # The red-first recipe in the root CLAUDE.md: `mktemp -d -t ot-redcheck.XXXXXX`. The ad hoc
    # `/tmp/redcheck*` trees it replaces were 4.1 GB on 2026-10-11, with no shape to sweep.
    _spec('red-check-tree', r'ot-redcheck\.[A-Za-z0-9]{6}', TIER_A, 'root CLAUDE.md recipe'),
    _spec(
        'integration-scratch',
        r'ot-integration-scratch\.[A-Za-z0-9]{6}',
        TIER_A,
        'scripts/run-integration-tests.sh',
    ),
]

#: Throwaway docker containers/networks tests start. A candidate needs ALL of: a test label
#: (below), a name matching one of these shapes, and a creation time before the cutoff.
#: Always Tier B: ``integration/throwaway_pg.py`` records the decision that a container is
#: never removed AUTOMATICALLY on this shared daemon, so only an operator's ``--execute``
#: removes one. Nothing here is ever selected by name alone.
TEST_DOCKER_LABELS = frozenset({'ot-test-artifact=1', 'ot-throwaway-pg=1'})
TEST_CONTAINER_SPECS: list[ArtifactSpec] = [
    _spec(
        'throwaway-postgres',
        rf'ot-(shared-throwaway-pg|cleanup-data-test|cleanup-test|restore600-netpg'
        rf'|migration-tcp)-{_H12}',
        TIER_B,
        'integration/conftest.py, test_cleanup_test_*_isolated*.py, scheduled backup '
        'roundtrip, test_v436_lite_settings_migration.py',
    ),
    _spec('throwaway-opensearch', rf'ot-vp658-os-{_H12}', TIER_B, 'voiceprint backup roundtrip'),
    _spec(
        'throwaway-redis',
        rf'ot-test-redis-(mps|backup-tasks)-{_H10}',
        TIER_B,
        'unit/test_migration_progress_service.py, unit/test_backup_tasks.py',
    ),
]
TEST_NETWORK_SPECS: list[ArtifactSpec] = [
    _spec('throwaway-network', rf'ot-(restore600|vp658)-net-{_H12}', TIER_B, 'roundtrip suites'),
]
#: Compose projects a test brings up under its own ``*-pytest-<uuid4>`` namespace
#: (unit/test_opentr_stop_container_scoping.py). Selected by the compose PROJECT LABEL, the
#: same scoping every stack script here uses, never by container name.
TEST_COMPOSE_PROJECT_SHAPE = re.compile(r'(opentranscribe|transcribe-app)-pytest-[0-9a-f]{32}')

#: Index names (exact) and prefixes that are never touched, whatever a shape says.
PROTECTED_INDEX_NAMES = frozenset({'transcripts', 'transcript_chunks', 'speakers'})
PROTECTED_INDEX_PREFIXES = (
    '.',  # .plugins-ml-*, .tasks, .opendistro*, .kibana: cluster/system indexes
    'transcript_chunks',
    'transcripts',
    'speakers',
    'audit-logs',
    'top_queries',  # query-insights plugin
    'security-auditlog',
)
PROTECTED_DATABASE_NAMES = frozenset({'postgres', 'template0', 'template1', 'opentranscribe'})
PROTECTED_BUCKET_NAMES = frozenset({'opentranscribe', 'transcribe-app'})


class Finding(NamedTuple):
    plane: str  # "index" | "database" | "bucket" | "tmp"
    name: str
    tier: str | None  # None = not a candidate
    reason: str  # why it is (or is not) a candidate


def match_spec(name: str, specs: list[ArtifactSpec]) -> ArtifactSpec | None:
    for spec in specs:
        if spec.shape.fullmatch(name):
            return spec
    return None


def is_protected_index(name: str) -> bool:
    return name in PROTECTED_INDEX_NAMES or name.startswith(PROTECTED_INDEX_PREFIXES)


def classify_index(
    name: str, docs_count: int | None, created_epoch: float | None, cutoff: int
) -> Finding:
    """Decide one index. Pure: no I/O."""
    if is_protected_index(name):
        return Finding('index', name, None, 'protected name: never swept')
    spec = match_spec(name, TEST_INDEX_SPECS)
    if spec is None:
        return Finding('index', name, None, 'no registered test shape')
    if created_epoch is None or created_epoch >= cutoff - GRACE_SECONDS:
        return Finding('index', name, None, 'created after the liveness cutoff: may be live')
    if docs_count is None:
        return Finding('index', name, None, 'document count unknown')
    if docs_count == 0:
        return Finding('index', name, TIER_A, f'empty {spec.label} index')
    return Finding('index', name, TIER_B, f'{spec.label} index holding {docs_count} doc(s)')


def classify_database(
    name: str, active_connections: int, protected: frozenset[str] | set[str]
) -> Finding:
    """Decide one Postgres database. Pure: no I/O. ``protected`` adds the target database."""
    if name in PROTECTED_DATABASE_NAMES or name in protected:
        return Finding('database', name, None, 'protected name: never swept')
    spec = match_spec(name, TEST_DATABASE_SPECS)
    if spec is None:
        return Finding('database', name, None, 'no registered test shape')
    if active_connections:
        return Finding('database', name, None, f'{active_connections} open connection(s)')
    return Finding('database', name, spec.tier, f'{spec.label} database, no connections')


def classify_bucket(
    name: str, created_epoch: float | None, cutoff: int, protected: frozenset[str] | set[str]
) -> Finding:
    """Decide one MinIO bucket. Pure: no I/O. ``protected`` adds the configured buckets."""
    if name in PROTECTED_BUCKET_NAMES or name in protected:
        return Finding('bucket', name, None, 'protected name: never swept')
    spec = match_spec(name, TEST_BUCKET_SPECS)
    if spec is None:
        return Finding('bucket', name, None, 'no registered test shape')
    if created_epoch is None or created_epoch >= cutoff - GRACE_SECONDS:
        return Finding('bucket', name, None, 'created after the liveness cutoff: may be live')
    return Finding('bucket', name, spec.tier, f'{spec.label} bucket')


def classify_tmp(name: str, owner_uid: int, mtime: float, cutoff: int, my_uid: int) -> Finding:
    """Decide one entry directly under the temp root. Pure: no I/O."""
    spec = match_spec(name, TEST_TMP_SPECS)
    if spec is None:
        return Finding('tmp', name, None, 'no registered test shape')
    if owner_uid != my_uid:
        return Finding('tmp', name, None, 'owned by another user')
    if mtime >= cutoff - GRACE_SECONDS:
        return Finding('tmp', name, None, 'modified after the liveness cutoff: may be live')
    return Finding('tmp', name, spec.tier, f'{spec.label} tree')


def classify_pipeline(name: str) -> Finding:
    """Decide one ingest pipeline. Pure. A pipeline has no creation time; it is only ever
    a candidate by registered shape, and creating one is the first thing its fixture does."""
    spec = match_spec(name, TEST_PIPELINE_SPECS)
    if spec is None:
        return Finding('pipeline', name, None, 'no registered test shape')
    return Finding('pipeline', name, spec.tier, f'{spec.label} ingest pipeline')


def classify_object(
    bucket: str, key: str, bucket_setting: str, modified_epoch: float | None, cutoff: int
) -> Finding:
    """Decide one object in a REAL bucket. Pure: no I/O."""
    full = f'{bucket}/{key}'
    for spec in TEST_OBJECT_SPECS:
        if spec.bucket_setting != bucket_setting or not spec.shape.fullmatch(key):
            continue
        if modified_epoch is None or modified_epoch >= cutoff - GRACE_SECONDS:
            return Finding('object', full, None, 'written after the liveness cutoff: may be live')
        return Finding('object', full, spec.tier, f'{spec.label} test object')
    return Finding('object', full, None, 'no registered test shape')


def classify_docker(
    kind: str, name: str, labels: dict[str, str], created_epoch: float | None, cutoff: int
) -> Finding:
    """Decide one container (``kind='container'``) or network. Pure: no I/O."""
    project = labels.get('com.docker.compose.project')
    if project is not None:
        if not TEST_COMPOSE_PROJECT_SHAPE.fullmatch(project):
            return Finding(kind, name, None, f'compose project {project!r}: not a test namespace')
        label_ok, spec_label = True, f'test compose project {project}'
    else:
        label_ok = any(f'{k}={v}' in TEST_DOCKER_LABELS for k, v in labels.items())
        specs = TEST_CONTAINER_SPECS if kind == 'container' else TEST_NETWORK_SPECS
        spec = match_spec(name, specs)
        if spec is None:
            return Finding(kind, name, None, 'no registered test shape')
        spec_label = spec.label
    if not label_ok:
        return Finding(kind, name, None, 'registered name but no test label: refused')
    if created_epoch is None or created_epoch >= cutoff - GRACE_SECONDS:
        return Finding(kind, name, None, 'created after the liveness cutoff: may be live')
    return Finding(kind, name, TIER_B, spec_label)


def selected(finding: Finding, *, include_tier_b: bool) -> bool:
    if finding.tier == TIER_A:
        return True
    return finding.tier == TIER_B and include_tier_b


# ---------------------------------------------------------------------------------------------
# Stack scoping: refuse to pair one stack's Postgres with another stack's stores.
# ---------------------------------------------------------------------------------------------

#: The default published port of each store; ``--fresh --port-offset N`` moves all of them by
#: the same N (``opentr.sh``'s ``FRESH_PORT_VARS``), so the offsets must agree.
BASE_PORTS = {'POSTGRES_PORT': 5176, 'MINIO_PORT': 5178, 'OPENSEARCH_PORT': 5180}


class ResolvedPort(NamedTuple):
    var: str
    port: int
    source: str  # "env" | "dotenv" | "default"


def port_scope_mismatch(ports: list[ResolvedPort]) -> str | None:
    """A reason to refuse, or None when every store resolves to the same stack.

    Only an EXPORTED value can disagree by accident: ``.env`` and the defaults describe one
    stack by construction. Exporting ``POSTGRES_PORT=5376`` for an isolated stack and
    forgetting ``OPENSEARCH_PORT`` would otherwise sweep the main stack's indexes while
    reporting the isolated stack's database.
    """
    if not any(p.source == 'env' for p in ports):
        return None
    offsets = {p.var: p.port - BASE_PORTS[p.var] for p in ports}
    if len(set(offsets.values())) <= 1:
        return None
    detail = ', '.join(f'{p.var}={p.port} ({p.source})' for p in ports)
    return (
        f'store ports do not resolve to one stack ({detail}); export every *_PORT for the '
        'stack you mean, as `opentr.sh --fresh --port-offset N` sets them'
    )


# ---------------------------------------------------------------------------------------------
# I/O planes. Each lists, classifies, and (only when told to) deletes through a re-check.
# ---------------------------------------------------------------------------------------------


class OpenSearchPlane:
    """Throwaway indexes and ingest pipelines on the target cluster."""

    _CAT = {'format': 'json', 'h': 'index,docs.count,creation.date'}

    def __init__(self, session: Any, base_url: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip('/')

    @staticmethod
    def _index_finding(row: dict[str, Any], cutoff: int) -> Finding:
        count = row.get('docs.count')
        created_ms = row.get('creation.date')
        return classify_index(
            row['index'],
            int(count) if count not in (None, '') else None,
            int(created_ms) / 1000 if created_ms not in (None, '') else None,
            cutoff,
        )

    def list_findings(self, cutoff: int) -> list[Finding]:
        resp = self.session.get(f'{self.base_url}/_cat/indices', params=self._CAT, timeout=30)
        resp.raise_for_status()
        findings = [self._index_finding(row, cutoff) for row in resp.json()]
        pipes = self.session.get(f'{self.base_url}/_ingest/pipeline', timeout=30)
        if pipes.status_code == 200:
            findings += [classify_pipeline(name) for name in sorted(pipes.json())]
        return findings

    def delete_one(self, finding: Finding, cutoff: int, *, include_tier_b: bool) -> bool:
        """Re-read and re-classify the ONE artifact, then delete it by exact name."""
        name = quote(finding.name, safe='')
        if finding.plane == 'pipeline':
            if not selected(classify_pipeline(finding.name), include_tier_b=include_tier_b):
                return False
            resp = self.session.delete(f'{self.base_url}/_ingest/pipeline/{name}', timeout=30)
            return resp.status_code in (200, 404)
        resp = self.session.get(
            f'{self.base_url}/_cat/indices/{name}', params=self._CAT, timeout=30
        )
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        rows = [r for r in resp.json() if r.get('index') == finding.name]
        if len(rows) != 1:
            return False
        if not selected(self._index_finding(rows[0], cutoff), include_tier_b=include_tier_b):
            return False
        deleted = self.session.delete(f'{self.base_url}/{name}', timeout=60)
        return deleted.status_code in (200, 404)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class PostgresDatabasePlane:
    """Scratch databases on the target Postgres server."""

    def __init__(self, engine: Any, target_db: str) -> None:
        self.engine = engine
        self.protected = {target_db}

    def _rows(self, conn: Any, only: str | None = None) -> list[tuple[str, int]]:
        from sqlalchemy import text

        sql = """
            SELECT d.datname,
                   (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) AS conns
            FROM pg_database d
            WHERE NOT d.datistemplate
        """
        params: dict[str, str] = {}
        if only is not None:
            sql += ' AND d.datname = :name'
            params['name'] = only
        return [(r.datname, int(r.conns)) for r in conn.execute(text(sql), params)]

    def list_findings(self, cutoff: int) -> list[Finding]:
        # A database has no creation time; liveness is "any open connection", which
        # classify_database refuses. ``cutoff`` keeps the plane interface uniform.
        del cutoff
        with self.engine.connect() as conn:
            return [classify_database(n, c, self.protected) for n, c in self._rows(conn)]

    def delete_one(self, finding: Finding, cutoff: int, *, include_tier_b: bool) -> bool:
        from sqlalchemy import text

        del cutoff
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            rows = self._rows(conn, only=finding.name)
            if not rows:
                return True
            fresh = classify_database(rows[0][0], rows[0][1], self.protected)
            if not selected(fresh, include_tier_b=include_tier_b):
                return False
            # Plain DROP, never WITH (FORCE): a connection opened since the re-check makes
            # this fail, which is the right outcome for a database someone started using.
            conn.execute(text(f'DROP DATABASE {_quote_ident(finding.name)}'))
        return True


class MinioPlane:
    """Throwaway buckets, plus test-keyed objects inside the REAL media/cache buckets."""

    def __init__(self, client: Any, protected: set[str], configured: dict[str, str]) -> None:
        self.client = client
        self.protected = protected
        #: setting name -> bucket name, e.g. {'MEDIA_BUCKET_NAME': 'opentranscribe'}
        self.configured = configured

    def _bucket_finding(self, bucket: Any, cutoff: int) -> Finding:
        created = bucket.creation_date.timestamp() if bucket.creation_date else None
        return classify_bucket(bucket.name, created, cutoff, self.protected)

    def _object_findings(self, cutoff: int, setting: str, prefix: str) -> list[Finding]:
        bucket = self.configured.get(setting)
        if not bucket or not self.client.bucket_exists(bucket):
            return []
        findings = []
        for obj in self.client.list_objects(bucket, prefix=prefix, recursive=True):
            modified = obj.last_modified.timestamp() if obj.last_modified else None
            finding = classify_object(bucket, obj.object_name, setting, modified, cutoff)
            if finding.tier is not None or 'registered' not in finding.reason:
                findings.append(finding)
        return findings

    def list_findings(self, cutoff: int) -> list[Finding]:
        findings = [self._bucket_finding(b, cutoff) for b in self.client.list_buckets()]
        for spec in TEST_OBJECT_SPECS:
            findings += self._object_findings(cutoff, spec.bucket_setting, spec.list_prefix)
        return findings

    def delete_one(self, finding: Finding, cutoff: int, *, include_tier_b: bool) -> bool:
        if finding.plane == 'object':
            bucket, _, key = finding.name.partition('/')
            setting = next((k for k, v in self.configured.items() if v == bucket), None)
            if setting is None:
                return False
            stat = self.client.stat_object(bucket, key)
            modified = stat.last_modified.timestamp() if stat.last_modified else None
            fresh = classify_object(bucket, key, setting, modified, cutoff)
            if not selected(fresh, include_tier_b=include_tier_b):
                return False
            self.client.remove_object(bucket, key)
            return True
        current = [b for b in self.client.list_buckets() if b.name == finding.name]
        if not current:
            return True
        if not selected(self._bucket_finding(current[0], cutoff), include_tier_b=include_tier_b):
            return False
        for obj in self.client.list_objects(finding.name, recursive=True):
            self.client.remove_object(finding.name, obj.object_name)
        self.client.remove_bucket(finding.name)
        return True


class TmpPlane:
    """Test trees and files directly under the system temp directory, owned by this user."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uid = os.getuid()

    def _classify(self, path: Path, cutoff: int) -> Finding | None:
        try:
            st = path.lstat()
        except OSError:
            return None
        return classify_tmp(path.name, st.st_uid, st.st_mtime, cutoff, self.uid)

    def list_findings(self, cutoff: int) -> list[Finding]:
        findings = []
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return []
        for path in entries:
            if match_spec(path.name, TEST_TMP_SPECS) is None:
                continue  # /tmp holds thousands of unrelated entries; report only ours
            finding = self._classify(path, cutoff)
            if finding is not None:
                findings.append(finding)
        return findings

    def delete_one(self, finding: Finding, cutoff: int, *, include_tier_b: bool) -> bool:
        path = self.root / finding.name
        if path.parent != self.root or '/' in finding.name:
            return False
        fresh = self._classify(path, cutoff)
        if fresh is None:
            return True
        if not selected(fresh, include_tier_b=include_tier_b):
            return False
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
        return not path.exists()


class DockerPlane:
    """Labelled throwaway containers/networks. Tier B only: never removed automatically.

    Lists ONLY what carries a test label (``docker ps --filter label=...``) or a compose
    project label, so nothing from an unrelated project on this shared daemon is ever even
    read, then classifies each by exact name/project shape and creation time.
    """

    def __init__(self, docker: str) -> None:
        self.docker = docker

    def _run(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed argv from a resolved docker binary
            [self.docker, *args], capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f'docker {args[0]} failed: {result.stderr.strip()}')
        return result.stdout

    def _inspect(self, kind: str, ref: str) -> tuple[str, dict[str, str], float | None]:
        fmt = '{{json .}}'
        data = json.loads(self._run(kind, 'inspect', '--format', fmt, ref))
        name = str(data.get('Name', '')).lstrip('/')
        if kind == 'container':
            labels = data.get('Config', {}).get('Labels') or {}
        else:
            labels = data.get('Labels') or {}
        return name, labels, _parse_docker_time(data.get('Created'))

    def _ids(self, kind: str) -> set[str]:
        ids: set[str] = set()
        filters = [f'label={label}' for label in sorted(TEST_DOCKER_LABELS)]
        if kind == 'container':
            filters.append('label=com.docker.compose.project')
        for flt in filters:
            if kind == 'container':
                out = self._run('ps', '-a', '-q', '--no-trunc', '--filter', flt)
            else:
                out = self._run('network', 'ls', '-q', '--no-trunc', '--filter', flt)
            ids.update(line for line in out.split() if line)
        return ids

    def list_findings(self, cutoff: int) -> list[Finding]:
        findings = []
        for kind in ('container', 'network'):
            for ref in sorted(self._ids(kind)):
                name, labels, created = self._inspect(kind, ref)
                finding = classify_docker(kind, name, labels, created, cutoff)
                # Compose-labelled containers from every OTHER project are read only to be
                # rejected; do not even print them.
                if finding.tier is not None or 'compose project' not in finding.reason:
                    findings.append(finding)
        return findings

    def delete_one(self, finding: Finding, cutoff: int, *, include_tier_b: bool) -> bool:
        kind = finding.plane
        name, labels, created = self._inspect(kind, finding.name)
        if name != finding.name:
            return False
        if not selected(
            classify_docker(kind, name, labels, created, cutoff), include_tier_b=include_tier_b
        ):
            return False
        if kind == 'container':
            self._run('rm', '-f', name)
        else:
            self._run('network', 'rm', name)
        return True


def _parse_docker_time(value: Any) -> float | None:
    """Docker's RFC 3339 timestamps carry nanoseconds, which ``fromisoformat`` rejects."""
    if not isinstance(value, str) or not value:
        return None
    trimmed = re.sub(r'\.(\d{6})\d*', r'.\1', value).replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(trimmed).timestamp()
    except ValueError:
        return None
