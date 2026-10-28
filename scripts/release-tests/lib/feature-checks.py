#!/usr/bin/env python3
"""Pure predicates behind lib/feature-checks.sh (the v0.6.0 feature checks of the rehearsal).

The shell side owns transport (curl, docker exec psql) and recording (as_record). This file
owns every YES/NO decision about a response, so each decision can be exercised with a good AND a
bad input (backend/tests/unit/test_release_feature_checks.py, selftest-feature-checks.sh) rather
than being read and trusted. A check whose bad input still passes is worse than no check.

CLI:   feature-checks.py PREDICATE [ARGS...] [< JSON on stdin]
Exit:  0 pass | 1 fail | 2 not applicable here (the caller records SKIP with the printed reason)
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
    if body.get('error'):
        return (
            SKIP,
            'audit log search is unavailable on this deployment (AUDIT_LOG_TO_OPENSEARCH off)',
        )
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


def stats_reranker(raw: str) -> Verdict:
    """``GET /system/stats`` carries the passage-reranker thread row (issue #1270)."""
    body = _load(raw)
    system = body.get('system') if isinstance(body, dict) else None
    row = system.get('chat_reranker_threads') if isinstance(system, dict) else None
    if not isinstance(row, dict) or not all(k in row for k in ('threads', 'source', 'setting')):
        return FAIL, 'system.chat_reranker_threads is absent or incomplete'
    return PASS, f'reranker row present (source={row["source"]})'


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
    'stats-reranker': stats_reranker,
    'aws-bucket': aws_bucket,
}
#: Predicates whose first argument is the stdin document rather than a positional argument.
STDIN_FIRST = {
    "columns", "backfill-record", "privacy-policy", "effective", "speaker-name", "purge-status",
    "purge-terminal", "audit-search", "sse-ready", "post-processing", "pp-settled", "policy-still-enabled", "stats-reranker", "aws-bucket",
}  # fmt: skip


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in PREDICATES:
        print(
            f'usage: {argv[0]} PREDICATE [ARGS...]; known: {", ".join(sorted(PREDICATES))}',
            file=sys.stderr,
        )
        return 2
    name, args = argv[1], argv[2:]
    if name in STDIN_FIRST:
        args = [sys.stdin.read(), *args]
    elif name == 'display-names':
        args = [Path(a).read_text(encoding='utf-8') if Path(a).exists() else '' for a in args]
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
