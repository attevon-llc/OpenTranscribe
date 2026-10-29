#!/usr/bin/env python3
"""Pure predicates behind lib/feature-checks.sh (the v0.6.0 feature checks of the rehearsal).

The shell side owns transport (curl, docker exec psql) and recording (as_record). This file
owns every YES/NO decision about a response, so each decision can be exercised with a good AND a
bad input (backend/tests/unit/test_release_feature_checks.py, selftest-feature-checks.sh) rather
than being read and trusted. A check whose bad input still passes is worse than no check.

CLI:   feature-checks.py PREDICATE [ARGS...] [< JSON on stdin]
Exit:  0 pass | 1 fail | 2 not applicable here (the caller records SKIP with the printed reason)
       | 64 usage error (unknown predicate) -- recorded as a FAIL, never a SKIP
Out:   one line of detail on stdout. Never a secret: nothing here receives a token or a key.

Stdlib only: the rehearsal runs on a bare host.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

PASS, FAIL, SKIP = 0, 1, 2
#: Exit code for a usage error (unknown predicate). Deliberately not SKIP's 2: the shell maps 2 to
#: "not applicable", and a typo'd predicate name must never read as a skipped check.
USAGE = 64
Verdict = tuple[int, str]

PURGE_AUDIT_EVENT = 'privacy.voiceprints.purge'
#: Terminal states a post-processing task may legitimately end in. ``failed`` is terminal but is a
#: defect, so it is deliberately NOT in this set.
GOOD_TERMINAL = {'done', 'skipped'}
SPEAKER_V449_COLUMNS = (
    'identity_source',
    'identity_source_at',
    'identity_source_user_id',
    'identity_source_detail',
    'suggestion_detail',
)


def _load(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def head(db_head: str, derived_head: str, min_rev: str) -> Verdict:
    """The database sits at the head derived from the migration graph, and that head is not older
    than the revision that introduced the v0.6.0 columns (a floor, not a pinned head)."""
    if not db_head or not derived_head:
        return FAIL, f'empty alembic head (db={db_head!r}, derived={derived_head!r})'
    if db_head != derived_head:
        return (
            FAIL,
            f"database is at {db_head!r} but the migration graph's head is {derived_head!r}",
        )
    match = re.match(r'^v(\d+)', derived_head)
    if not match or int(match.group(1)) < int(min_rev):
        return FAIL, f'derived head {derived_head!r} is older than v{min_rev}'
    return PASS, f'database at the derived head {derived_head}'


def columns(found: str) -> Verdict:
    """``found`` is the newline list of speaker columns the database reported."""
    have = {line.strip() for line in found.splitlines() if line.strip()}
    missing = [c for c in SPEAKER_V449_COLUMNS if c not in have]
    if missing:
        return FAIL, f'speaker is missing column(s): {", ".join(missing)}'
    return PASS, 'speaker carries all five v449 columns'


def backfill_record(value: str) -> Verdict:
    """``system_settings.privacy.identity_source_backfill`` exists and records its counts."""
    if not value.strip():
        return FAIL, 'system_settings has no privacy.identity_source_backfill row after the upgrade'
    record = _load(value)
    if not isinstance(record, dict):
        return FAIL, 'privacy.identity_source_backfill is not a JSON object'
    for key in ('revision', 'stamped', 'shape_left_unmarked', 'recorded_at'):
        if key not in record:
            return FAIL, f'privacy.identity_source_backfill lacks {key!r}'
    if not isinstance(record['stamped'], int) or isinstance(record['stamped'], bool):
        return FAIL, 'privacy.identity_source_backfill.stamped is not an integer'
    return PASS, f'backfill recorded: stamped={record["stamped"]}'


def display_names(pre: str, post: str) -> Verdict:
    """Every speaker present before the upgrade keeps its display_name. ``id|name`` lines."""
    if pre.lstrip().startswith('('):
        return SKIP, 'the pre-upgrade snapshot could not read speaker.display_name (older schema)'

    def parse(text: str) -> dict[str, str]:
        rows: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition('|')
            if sep:
                rows[key] = value
        return rows

    before, after = parse(pre), parse(post)
    if not before:
        return FAIL, 'no speakers existed before the upgrade, so nothing was proven'
    lost = [k for k in before if k not in after]
    changed = [k for k, v in before.items() if k in after and after[k] != v]
    if lost or changed:
        return FAIL, f'speakers lost={lost[:5]} display_name changed={changed[:5]}'
    named = sum(1 for v in before.values() if v != '<null>')
    return PASS, f'{len(before)} pre-upgrade speakers kept their display_name ({named} named)'


def privacy_policy(raw: str, voice: str = '', gender: str = '') -> Verdict:
    """``GET|PUT /admin/privacy-policy`` answers both switches as real booleans."""
    body = _load(raw)
    if not isinstance(body, dict):
        return FAIL, 'not a JSON object'
    for key in ('voiceprint_matching_enabled', 'gender_inference_enabled'):
        if not _is_bool(body.get(key)):
            return FAIL, f'{key} is {body.get(key)!r}, expected a boolean'
    for key, want in (('voiceprint_matching_enabled', voice), ('gender_inference_enabled', gender)):
        if want and str(body[key]).lower() != want:
            return FAIL, f'{key} is {body[key]!r}, expected {want}'
    return (
        PASS,
        f'voiceprints={body["voiceprint_matching_enabled"]} gender={body["gender_inference_enabled"]}',
    )


def effective(raw: str) -> Verdict:
    """``GET /privacy/effective`` shape; ``locked_by`` is null exactly when enabled."""
    body = _load(raw)
    if not isinstance(body, dict) or 'organization_uuid' not in body:
        return FAIL, 'not an EffectivePrivacy object (organization_uuid key absent)'
    if body['organization_uuid'] is not None:
        return FAIL, 'organization_uuid is set on a community install'
    for feature in ('voiceprint_matching', 'gender_inference'):
        decision = body.get(feature)
        if not isinstance(decision, dict) or not _is_bool(decision.get('enabled')):
            return FAIL, f'{feature}.enabled is not a boolean'
        if decision['enabled'] != (decision.get('locked_by') is None):
            return FAIL, f'{feature}: locked_by must be null exactly when enabled'
    return PASS, 'effective policy has the expected shape'


def speaker_name(raw: str, speaker_uuid: str, name: str, mode: str) -> Verdict:
    """In ``GET /files/{uuid}``, a voice-made name is shown (``visible``) or withheld (``hidden``)."""
    body = _load(raw)
    speakers = body.get('speakers') if isinstance(body, dict) else None
    if not isinstance(speakers, list):
        return FAIL, 'file detail has no speakers list'
    row = next((s for s in speakers if isinstance(s, dict) and s.get('uuid') == speaker_uuid), None)
    if row is None:
        return FAIL, 'the seeded speaker is not in the file detail'
    identity = row.get('identity')
    if mode == 'visible':
        source = identity.get('source') if isinstance(identity, dict) else None
        if row.get('display_name') != name or source != 'voiceprint':
            return (
                FAIL,
                f'expected the voice-made name with identity.source=voiceprint, got source={source!r}',
            )
        return PASS, 'voice-made name and its provenance are shown'
    if row.get('display_name') == name:
        return FAIL, 'the voice-made name is still returned while voiceprints are off'
    if identity is not None:
        return FAIL, 'identity (how the voice-made name came to be) is still returned'
    return PASS, 'voice-made name and its provenance are withheld'


def purge_status(raw: str, min_reverted: str = '0') -> Verdict:
    """A finished purge reports per-store counts, a verified-zero residual and its audit ref."""
    body = _load(raw)
    if not isinstance(body, dict):
        return FAIL, 'not a JSON object'
    if body.get('status') != 'completed':
        return FAIL, f'purge status is {body.get("status")!r}, expected completed'
    counts = body.get('counts')
    if not isinstance(counts, dict) or not all(
        isinstance(counts.get(k), dict) for k in ('postgres', 'opensearch')
    ):
        return FAIL, 'counts is not grouped per store (postgres, opensearch)'
    reverted = counts['postgres'].get('speaker_identities_reverted')
    if not isinstance(reverted, int) or reverted < int(min_reverted):
        return (
            FAIL,
            f'postgres.speaker_identities_reverted={reverted!r}, expected >= {min_reverted}',
        )
    if body.get('residual_ok') is not True:
        return FAIL, f'residual_ok={body.get("residual_ok")!r} residual={body.get("residual")!r}'
    if body.get('residual_unverified'):
        return FAIL, f'residual measures not taken: {body.get("residual_unverified")}'
    audit = body.get('audit')
    if not isinstance(audit, list) or not any(
        isinstance(a, dict) and a.get('event_type') == PURGE_AUDIT_EVENT for a in audit
    ):
        return FAIL, f'status carries no {PURGE_AUDIT_EVENT} audit reference'
    return PASS, f'purge completed, residual_ok, reverted={reverted}'


def purge_terminal(raw: str) -> Verdict:
    """Exit 0 once the purge is finished (either way), 1 while it is still running."""
    body = _load(raw)
    state = body.get('status') if isinstance(body, dict) else None
    return (PASS if state in ('completed', 'failed') else FAIL), f'status={state}'


def audit_search(raw: str) -> Verdict:
    """The audit store holds an event for the purge. Not applicable when audit-to-OpenSearch is off."""
    body = _load(raw)
    if not isinstance(body, dict):
        return FAIL, 'not a JSON object'
    error = body.get('error')
    if error:
        # Only the documented "audit-to-OpenSearch is off" answer is not applicable. Any other
        # error (the store failing the query, "An internal error occurred while querying audit
        # logs.") is a defect in what this check exists to verify, not an absent feature.
        if 'AUDIT_LOG_TO_OPENSEARCH' in str(error):
            return (
                SKIP,
                'audit log search is unavailable on this deployment (AUDIT_LOG_TO_OPENSEARCH off)',
            )
        return FAIL, f'the audit store answered with an error: {str(error)[:120]}'

    total = body.get('total')
    if not isinstance(total, int) or total < 1:
        return FAIL, f'no {PURGE_AUDIT_EVENT} event in the audit store (total={total!r})'
    return PASS, f'{total} {PURGE_AUDIT_EVENT} event(s) in the audit store'


def export_file(fmt: str, path: str) -> Verdict:
    """The bytes of an export are a real file of that format, not an error page or an empty body."""
    data = Path(path).read_bytes()
    if not data.strip():
        return FAIL, f'{fmt}: empty body'
    text = data.decode('utf-8', 'replace')
    if fmt == 'docx':
        ok = data[:2] == b'PK'
    elif fmt == 'pdf':
        ok = data[:5] == b'%PDF-'
    elif fmt == 'json':
        ok = _load(text) is not None
    elif fmt == 'csv':
        ok = text.splitlines()[0].startswith('Start Time,End Time,Speaker,Text')
    elif fmt == 'srt':
        ok = '-->' in text and text.lstrip()[:1].isdigit()
    elif fmt == 'vtt':
        ok = text.startswith('WEBVTT')
    elif fmt == 'md':
        ok = any(line.startswith('# ') for line in text.splitlines())
    elif fmt == 'txt':
        ok = not text.lstrip().startswith(('{', '<'))
    else:
        return FAIL, f'unknown export format {fmt!r}'
    return (PASS, f'{fmt}: {len(data)} bytes') if ok else (FAIL, f'{fmt}: body is not a {fmt} file')


def sse_ready(text: str) -> Verdict:
    """The download-job stream delivered ``ready`` with a URL (printed) instead of ``error``."""
    event = ''
    for line in text.splitlines():
        if line.startswith('event:'):
            event = line.split(':', 1)[1].strip()
        elif line.startswith('data:') and event in ('ready', 'error'):
            payload = _load(line.split(':', 1)[1].strip())
            if event == 'error':
                code = payload.get('code') if isinstance(payload, dict) else None
                return FAIL, f'export job failed (code={code!r})'
            url = payload.get('url') if isinstance(payload, dict) else None
            if isinstance(url, str) and url.startswith('http'):
                return PASS, url
            return FAIL, 'ready frame carried no URL'
    return FAIL, 'the stream ended without a ready frame (job did not finish in time)'


def post_processing(
    raw: str, speakers: str = '0', predicted: str = '0', max_predicted: str = ''
) -> Verdict:
    """Every reported task reached ``done``/``skipped``; the Gender row skipped on pre-existing attributes.

    ``speakers``/``predicted`` count the file's speakers and those carrying attributes;
    ``max_predicted`` is the newest ``attributes_predicted_at`` as epoch seconds.
    """
    body = _load(raw)
    if not isinstance(body, dict) or not isinstance(body.get('tasks'), dict):
        return FAIL, 'not a post-processing status object'
    if body.get('run_at') is None or not body['tasks']:
        return FAIL, 'no post-processing run is known for the file (run_at null or no tasks)'
    bad = {
        k: v.get('state') for k, v in body['tasks'].items() if v.get('state') not in GOOD_TERMINAL
    }
    if bad:
        return FAIL, f'task(s) not finished cleanly: {bad}'
    attrs = body['tasks'].get('speaker_attributes')
    if attrs is None:
        return FAIL, 'no speaker_attributes (Gender) row was reported'
    all_pre = int(speakers) > 0 and int(speakers) == int(predicted)
    if attrs['state'] == 'skipped':
        return PASS, 'all tasks finished; Gender row is skipped'
    started = attrs.get('started_at')
    if all_pre and max_predicted and (started is None or float(max_predicted) <= float(started)):
        return (
            FAIL,
            'speaker attributes existed before the Gender row ran, yet it did not report skipped',
        )
    if all_pre:
        return (
            SKIP,
            'all tasks finished; detection itself wrote the attributes, so the already-present branch was not exercised',
        )
    return PASS, 'all tasks finished'


def pp_settled(raw: str) -> Verdict:
    """Exit 0 once a run is known and no task is still ``queued``/``started`` (poll condition)."""
    body = _load(raw)
    tasks = body.get('tasks') if isinstance(body, dict) else None
    if not isinstance(tasks, dict) or not tasks or body.get('run_at') is None:
        return FAIL, 'no post-processing run reported yet'
    open_tasks = [k for k, v in tasks.items() if v.get('state') in ('queued', 'started')]
    return (FAIL, f'still running: {open_tasks}') if open_tasks else (PASS, 'settled')


def policy_still_enabled(raw: str) -> Verdict:
    """The 409 body of a purge attempted while the switch is on: ``detail.code`` names why."""
    body = _load(raw)
    detail = body.get('detail') if isinstance(body, dict) else None
    code = detail.get('code') if isinstance(detail, dict) else None
    if code != 'policy_still_enabled':
        return FAIL, f'409 detail.code is {code!r}, expected policy_still_enabled'
    return PASS, 'purge refused while the switch is on'


def aws_bucket(raw: str) -> Verdict:
    """The AWS ASR connection test names the missing bucket variable instead of reaching the network."""
    body = _load(raw)
    if not isinstance(body, dict):
        return FAIL, 'not a JSON object'
    if body.get('success') is not False:
        return FAIL, f'success={body.get("success")!r}, expected false with no bucket configured'
    if 'AWS_TRANSCRIBE_BUCKET' not in str(body.get('message', '')):
        return FAIL, 'the failure message does not name AWS_TRANSCRIBE_BUCKET'
    return PASS, 'AWS_TRANSCRIBE_BUCKET is named in the failure'


#: The measures a voiceprint purge must report, all 0: the Postgres ones plus one per OpenSearch
#: document kind. A purge whose status carries fewer measures verified less than it claims.
PURGE_RESIDUAL_KEYS = (
    'speaker_identities_machine',
    'speaker_identity_provenance',
    'voiceprint_suggestions',
    'voiceprint_suggestion_details',
    'speaker_matches',
    'unpromoted_cluster_members',
    'profiles_with_embeddings',
    'speaker_docs',
    'profile_docs',
    'cluster_docs',
)
#: Log lines that mean the broken torchcodec audio backend was reached (root CLAUDE.md audio.py).
TORCHCODEC_LOG_MARKERS = ('libtorchcodec', 'torchcodec is not installed correctly')
V448_TASK_EXPECTED = {
    # id: (status, error_message startswith)
    'rehearsal-v448-A': ('failed', 'superseded by rehearsal-v448-B'),
    'rehearsal-v448-B': ('in_progress', ''),
    'rehearsal-v448-C': ('failed', 'superseded by rehearsal-v448-D'),
    'rehearsal-v448-D': ('completed', ''),
}
COUNT_TABLES = ('media_file', 'transcript_segment', 'speaker', 'task')


def reranker_cap(raw: str, env_setting: str = '') -> Verdict:
    """The reranker row reports a cap that matches the deployment setting the process runs with.

    ``env_setting`` is ``CHAT_RERANKER_TORCH_THREADS`` read from the backend container (empty =
    unset = ``auto``). Presence of the row is not enough: it must be the SETTING that reached the
    process, and the number must be a positive integer unless the operator's torch env governs.
    """
    body = _load(raw)
    system = body.get('system') if isinstance(body, dict) else None
    row = system.get('chat_reranker_threads') if isinstance(system, dict) else None
    if not isinstance(row, dict) or not all(k in row for k in ('threads', 'source', 'setting')):
        return FAIL, 'system.chat_reranker_threads is absent or incomplete'
    want = (env_setting or 'auto').strip().lower()
    if str(row['setting']).strip().lower() != want:
        return (
            FAIL,
            f'process reports setting {row["setting"]!r} but the container env says {want!r}',
        )
    threads, source = row['threads'], row['source']
    if source == 'torch_env':
        if threads is not None:
            return FAIL, 'source torch_env must report threads=null (no cap applied here)'
        return PASS, 'OMP/TORCH_NUM_THREADS governs; no cap applied by the reranker'
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        return FAIL, f'threads={threads!r} is not a positive integer (source={source!r})'
    if want.isdigit():
        if source != 'configured' or threads != int(want):
            return FAIL, f'setting {want} resolved to threads={threads} source={source}'
    elif want == 'auto' and (source != 'auto' or threads > 8):
        return FAIL, f'auto resolved to threads={threads} source={source} (ceiling is 8)'
    return PASS, f'cap applied: threads={threads} source={source}'


def os_labels(raw: str, name: str, mode: str) -> Verdict:
    """The chunk index holds (``present``) or no longer holds (``absent``) a speaker label."""
    body = _load(raw)
    if not isinstance(body, dict) or body.get('error'):
        return FAIL, f'probe failed: {body.get("error") if isinstance(body, dict) else raw[:80]}'
    labels = body.get('labels')
    if not isinstance(labels, list) or not body.get('indexed'):
        return FAIL, 'the file has no chunk documents in the index'
    held = name in labels
    if mode == 'present':
        return (
            (PASS, 'index holds the voice-made label')
            if held
            else (FAIL, f'index labels {labels} lack {name!r}')
        )
    if mode == 'absent':
        return (
            (FAIL, f'index still holds {name!r}')
            if held
            else (PASS, f'index no longer holds the voice-made label ({labels})')
        )
    return FAIL, f'unknown mode {mode!r}'


def drift_scan(raw: str, name: str, mode: str) -> Verdict:
    """The app's own drift scan, read-only. ``drift``: the file's labels disagree; ``clean``: agree."""
    body = _load(raw)
    if not isinstance(body, dict) or body.get('error'):
        return FAIL, f'probe failed: {body.get("error") if isinstance(body, dict) else raw[:80]}'
    for key in ('scanned', 'drifted', 'expected', 'indexed'):
        if key not in body:
            return FAIL, f'drift probe lacks {key!r}'
    agree = body['expected'] == body['indexed']
    if mode == 'drift':
        if agree or body['drifted'] < 1:
            return (
                FAIL,
                f'expected a stale index but expected={body["expected"]} indexed={body["indexed"]} drifted={body["drifted"]}',
            )
        return PASS, f'drift detected: expected={body["expected"]} indexed={body["indexed"]}'
    if mode == 'clean':
        if not agree:
            return FAIL, f'labels disagree: expected={body["expected"]} indexed={body["indexed"]}'
        if name in body['expected'] and name not in body['indexed']:
            return FAIL, 'voice-made label expected but not indexed'
        return PASS, f'no drift for the file ({body["scanned"]} scanned)'
    return FAIL, f'unknown mode {mode!r}'


def os_seed(raw: str) -> Verdict:
    """Unstamped speaker documents and a decoy were really written to a concrete index."""
    body = _load(raw)
    if not isinstance(body, dict) or body.get('error'):
        return FAIL, f'probe failed: {body.get("error") if isinstance(body, dict) else raw[:80]}'
    if not body.get('seeded') or not body.get('decoy'):
        return FAIL, 'no speaker index accepted the seed documents (nothing to purge-test)'
    return PASS, f'seeded {len(body["seeded"])} index document(s) plus a decoy'


def os_docs(raw: str, speaker: str, decoy: str, mode: str) -> Verdict:
    """``before``: the seeded doc and the decoy exist. ``after``: the doc is gone, the decoy stays."""
    body = _load(raw)
    counts = body.get('counts') if isinstance(body, dict) else None
    if not isinstance(counts, dict) or body.get('error'):
        return FAIL, 'count probe failed'
    mine, other = counts.get(speaker), counts.get(decoy)
    if mine is None or other is None:
        return FAIL, 'a document count could not be measured (null)'
    if mode == 'before':
        if mine < 1 or other < 1:
            return FAIL, f'seed control failed: speaker={mine} decoy={other}'
        return PASS, f'seed control: speaker doc x{mine}, decoy x{other}'
    if mine != 0:
        return FAIL, f'{mine} document(s) of the purged speaker survive in OpenSearch'
    if other != 1:
        return FAIL, f'the decoy (outside the purged scope) count is {other}, expected 1'
    return PASS, 'purged speaker documents gone from every index; out-of-scope decoy kept'


def purge_residual_complete(raw: str) -> Verdict:
    """The status carries every residual measure, each 0, including all three OpenSearch kinds."""
    body = _load(raw)
    residual = body.get('residual') if isinstance(body, dict) else None
    if not isinstance(residual, dict):
        return FAIL, 'status has no residual object'
    missing = [k for k in PURGE_RESIDUAL_KEYS if k not in residual]
    if missing:
        return FAIL, f'residual lacks measure(s): {missing}'
    left = {k: residual[k] for k in PURGE_RESIDUAL_KEYS if residual[k] != 0}
    if left:
        return FAIL, f'residual not zero: {left}'
    return PASS, f'all {len(PURGE_RESIDUAL_KEYS)} residual measures are 0'


def _kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep:
            out[key.strip()] = value.strip()
    return out


def upgrade_counts(pre: str, post: str) -> Verdict:
    """The upgrade ran over a POPULATED database and lost no row (counts before and after)."""
    before, after = _kv(pre), _kv(post)
    if not before or not after:
        return FAIL, 'a row-count snapshot is missing'
    report = []
    for table in COUNT_TABLES:
        try:
            b, a = int(before[table]), int(after[table])
        except (KeyError, ValueError):
            return (
                FAIL,
                f'{table}: count unreadable (pre={before.get(table)!r}, post={after.get(table)!r})',
            )
        if b < 1:
            return FAIL, f'{table} was empty before the upgrade: the migration ran over nothing'
        if a < b:
            return FAIL, f'{table} lost rows across the upgrade: {b} -> {a}'
        report.append(f'{table} {b}->{a}')
    return PASS, ', '.join(report)


def backfill_consistent(raw: str, actual: str) -> Verdict:
    """The v449 record's ``stamped`` equals the speakers that actually carry an identity_source."""
    record = _load(raw)
    if not isinstance(record, dict) or not isinstance(record.get('stamped'), int):
        return FAIL, 'no readable privacy.identity_source_backfill record'
    try:
        found = int(_kv(actual)['identity_source_not_null'])
    except (KeyError, ValueError):
        return FAIL, 'post-upgrade identity_source count unreadable'
    if found != record['stamped']:
        return FAIL, f'record says stamped={record["stamped"]} but {found} speaker row(s) carry one'
    return PASS, f'record and table agree: {found} stamped'


def v448_tasks(raw: str) -> Verdict:
    """The duplicate/superseded task rows seeded BEFORE the upgrade were resolved by v448."""
    if raw.lstrip().startswith('('):
        return FAIL, f'the pre-upgrade seed did not land: {raw.strip()[:120]}'
    rows: dict[str, tuple[str, str]] = {}
    for line in raw.splitlines():
        parts = line.split('|', 2)
        if len(parts) == 3:
            rows[parts[0]] = (parts[1], parts[2])
    for task_id, (status, prefix) in V448_TASK_EXPECTED.items():
        got = rows.get(task_id)
        if got is None:
            return FAIL, f'{task_id} is gone (v448 must never delete rows)'
        if got[0] != status or not got[1].startswith(prefix):
            return (
                FAIL,
                f'{task_id}: status={got[0]!r} error={got[1]!r}, expected {status!r} {prefix!r}',
            )
    return PASS, 'older duplicate and superseded rows failed with the superseding id; newest kept'


def v448_guard(raw: str, index_def: str, duplicates: str) -> Verdict:
    """The partial UNIQUE index exists, no active duplicate remains, and a duplicate is refused."""
    lowered = index_def.lower()
    if 'unique' not in lowered or 'media_file_id' not in lowered or 'pending' not in lowered:
        return (
            FAIL,
            f'uq_task_active_per_file_type is missing or not the partial unique index: {index_def[:100]!r}',
        )
    if duplicates.strip() != '0':
        return (
            FAIL,
            f'{duplicates.strip()!r} (file, step) pair(s) still hold more than one active row',
        )
    if 'uq_task_active_per_file_type' not in raw and 'duplicate key' not in raw:
        return FAIL, f'a second active row was NOT refused: {raw.strip()[:120]!r}'
    return (
        PASS,
        'index present, no active duplicates, a duplicate insert is refused by the database',
    )


def gpu_probe(raw: str) -> Verdict:
    """The worker sees CUDA, torchcodec/torchaudio import, and the app's loader avoids torchcodec."""
    body = _load(raw)
    if not isinstance(body, dict) or body.get('error'):
        return FAIL, f'probe failed: {body.get("error") if isinstance(body, dict) else raw[:80]}'
    if body.get('cuda_available') is not True or not body.get('device_name'):
        return (
            FAIL,
            f'the worker sees no CUDA device ({body.get("torch_error") or "cuda_available false"})',
        )
    for key in ('torchcodec_import', 'torchaudio_import'):
        if body.get(key) != 'ok':
            return FAIL, f'{key}: {body.get(key)!r}'
    if body.get('audio_samples') != 16000:
        return (
            FAIL,
            f'the app audio loader returned {body.get("audio_samples")!r} samples ({body.get("audio_error")})',
        )
    if body.get('app_loader_pulled_torchcodec') is not False:
        return FAIL, 'the app audio loader imported torchcodec'
    return (
        PASS,
        f'CUDA on {body["device_name"]}; torchcodec/torchaudio import; loader avoids torchcodec',
    )


def pipeline_evidence(raw: str) -> Verdict:
    """``status|segments|speakers|provider`` of the newest transcribed file: a real GPU pipeline run."""
    parts = raw.strip().split('|')
    if len(parts) != 4:
        return FAIL, f'no completed transcription found ({raw.strip()[:80]!r})'
    status, segments, speakers, provider = parts
    if status.lower() != 'completed':
        return FAIL, f'file status is {status!r}'
    if not segments.isdigit() or int(segments) < 1:
        return FAIL, f'{segments!r} transcript segments'
    if not speakers.isdigit() or int(speakers) < 1:
        return FAIL, f'{speakers!r} diarized speakers'
    if not provider:
        return FAIL, 'diarization_provider is empty: diarization never resolved an engine'
    return PASS, f'{segments} segments, {speakers} speakers, diarized by {provider}'


def worker_log_clean(text: str) -> Verdict:
    """The worker log never reports the broken torchcodec backend."""
    hits = [ln for ln in text.splitlines() if any(m in ln.lower() for m in TORCHCODEC_LOG_MARKERS)]
    if hits:
        return FAIL, f'{len(hits)} torchcodec error line(s), first: {hits[0][:160]}'
    if not text.strip():
        return FAIL, 'the worker log is empty, so nothing was checked'
    return PASS, 'no torchcodec failure in the worker log'


PREDICATES: dict[str, Callable[..., Verdict]] = {
    'head': head,
    'columns': columns,
    'backfill-record': backfill_record,
    'display-names': display_names,
    'privacy-policy': privacy_policy,
    'effective': effective,
    'speaker-name': speaker_name,
    'purge-status': purge_status,
    'purge-terminal': purge_terminal,
    'audit-search': audit_search,
    'export-file': export_file,
    'sse-ready': sse_ready,
    'post-processing': post_processing,
    'pp-settled': pp_settled,
    'policy-still-enabled': policy_still_enabled,
    'aws-bucket': aws_bucket,
    'reranker-cap': reranker_cap,
    'os-labels': os_labels,
    'drift-scan': drift_scan,
    'os-seed': os_seed,
    'os-docs': os_docs,
    'purge-residual-complete': purge_residual_complete,
    'upgrade-counts': upgrade_counts,
    'backfill-consistent': backfill_consistent,
    'v448-tasks': v448_tasks,
    'v448-guard': v448_guard,
    'gpu-probe': gpu_probe,
    'pipeline-evidence': pipeline_evidence,
    'worker-log-clean': worker_log_clean,
}
#: Predicates whose first argument is the stdin document rather than a positional argument.
STDIN_FIRST = {
    "columns", "backfill-record", "privacy-policy", "effective", "speaker-name", "purge-status",
    "purge-terminal", "audit-search", "sse-ready", "post-processing", "pp-settled", "policy-still-enabled", "aws-bucket",
    "reranker-cap", "os-labels", "drift-scan", "os-seed", "os-docs", "purge-residual-complete", "backfill-consistent",
    "v448-tasks", "v448-guard", "gpu-probe", "pipeline-evidence", "worker-log-clean",
}  # fmt: skip


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in PREDICATES:
        print(
            f'usage: {argv[0]} PREDICATE [ARGS...]; known: {", ".join(sorted(PREDICATES))}',
            file=sys.stderr,
        )
        return USAGE
    name, args = argv[1], argv[2:]
    if name in STDIN_FIRST:
        args = [sys.stdin.read(), *args]
    elif name in ('display-names', 'upgrade-counts'):
        args = [Path(a).read_text(encoding='utf-8') if Path(a).exists() else '' for a in args]
    if name == 'backfill-consistent' and len(args) > 1:
        args = [
            args[0],
            Path(args[1]).read_text(encoding='utf-8') if Path(args[1]).exists() else '',
        ]
    try:
        code, detail = PREDICATES[name](*args)
    except (TypeError, ValueError, IndexError, OSError) as exc:
        # A malformed input is a failed check, never an uncaught traceback that the shell would
        # read as "predicate crashed, so skip".
        code, detail = FAIL, f'{name}: could not evaluate ({type(exc).__name__}: {exc})'
    print(detail)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
