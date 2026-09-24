#!/usr/bin/env python3
"""#532 follow-up Phase 0 (Unit U9): the offline, no-GPU, no-LLM content oracle.

``docs/design/532_hybrid_summary_synthesis_plan.md`` section 4.1. Decides — cheaply, before
any GPU time is spent — which digest section to keep in the hybrid overview entry (R-sec),
whether to include structured decisions/action items (R-items), and whether the hybrid design
can be killed outright (K0) before a single LLM call runs.

For every (series, shape) question in the U8 expanded question set and every file in that
question's series, this builds each CANDIDATE entry text a map-tier overview entry could show
(see the table in the module docstring below) and scores it against that FILE's own tagged
AMI reference items with :func:`tests.eval.harness.ami_recall.score_answer` — a pure lexical
floor, never an LLM call. ``structured_summary_text``/``_summary_highlight_text`` are IMPORTED
from the real app code (never re-implemented here), so the oracle scores exactly what would
ship.

Candidates (one row per composition):

======  ========================================================================
Code    Entry text
======  ========================================================================
C       ``" ".join(S[:3])`` — the control (leading digest sections)
S0      First digest section alone
Smid    ``S[len(S)//2]`` — the middle section alone
Slast   Last digest section alone (the proposed CLOSING section)
P       ``_summary_highlight_text(summary_data)`` — arm (d)'s paragraph
PI      ``structured_summary_text(summary_data, budget=inf)`` — paragraph + items, uncapped
H1      P + Slast, budget-capped per plan section 2.3 (the literal pre-registered hybrid)
H2      PI + Slast, budget-capped per plan section 2.3 (the PROPOSED hybrid)
======  ========================================================================

Usage::

    python3 scripts/overview_content_oracle.py \\
        --question-set .rag-403/probe-runs/multi-file-expanded-v060synth.json \\
        --pg-container otfresh-v060synth-postgres \\
        --out backend/tests/eval/baselines/probe-532-oracle
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger('overview_content_oracle')

#: Matches build_probe_question_set.py's own convention.
TITLE_PREFIX = 'QMSum Product — '

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'backend'))
sys.path.insert(0, str(REPO_ROOT / 'backend' / 'tests'))

CANDIDATE_CODES = ('C', 'S0', 'Smid', 'Slast', 'P', 'PI', 'H1', 'H2')


def _load_app_functions():
    """Import the real production composition functions — never re-implemented here."""
    from app.services.chat.citations import DIGEST_SNIPPET_CHARS
    from app.services.chat.mapreduce.file_summaries import (
        _summary_highlight_text,
        structured_summary_text,
    )
    from app.services.chat.mapreduce.overview import sections_budget
    from app.services.chat.prompting import _cut_at_boundary

    return {
        'DIGEST_SNIPPET_CHARS': DIGEST_SNIPPET_CHARS,
        '_summary_highlight_text': _summary_highlight_text,
        'structured_summary_text': structured_summary_text,
        'sections_budget': sections_budget,
        '_cut_at_boundary': _cut_at_boundary,
    }


def fetch_files(container: str, file_uuids: list[str]) -> dict[str, dict[str, Any]]:
    """One batched read of every file this question set actually needs.

    Returns:
        ``uuid -> {title, summary_status, summary_data, digest, source_fingerprint}``.
        A uuid the query does not return (no ``media_file``/``file_facts`` row) is simply
        absent — the caller treats that as "not usable", never a fabricated empty entry.
    """
    if not file_uuids:
        return {}
    uuids_sql = ','.join(f"'{u}'" for u in sorted(set(file_uuids)))
    sql = (
        'SELECT mf.uuid, mf.title, mf.summary_status, mf.summary_data::text, '
        'ff.digest::text, ff.source_fingerprint '
        'FROM media_file mf JOIN file_facts ff ON ff.media_file_id = mf.id '
        f'WHERE mf.uuid IN ({uuids_sql});'
    )  # noqa: S608 — uuids are our own generated values, not user input
    proc = subprocess.run(  # noqa: S603
        [
            'docker',
            'exec',
            container,
            'psql',
            '-U',
            'postgres',
            '-d',
            'opentranscribe',
            '-tAF',
            '\x1f',
            '-c',
            sql,
        ],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f'could not read files from {container}: {proc.stderr.strip()}')

    out: dict[str, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split('\x1f')
        if len(parts) != 6:
            continue
        uuid, title, status, summary_json, digest_json, fingerprint = parts
        out[uuid] = {
            'title': title,
            'summary_status': status or None,
            'summary_data': json.loads(summary_json) if summary_json else {},
            'digest': json.loads(digest_json) if digest_json else {},
            'source_fingerprint': fingerprint or None,
        }
    return out


def _sections(file_row: dict[str, Any]) -> list[str]:
    raw = (file_row.get('digest') or {}).get('sections') or []
    ordered = sorted(raw, key=lambda s: s.get('index', 0))
    return [str(s.get('text') or '') for s in ordered]


def build_candidates(
    file_row: dict[str, Any], *, n_files_in_scope: int, app: dict[str, Any]
) -> dict[str, str]:
    """Every candidate entry text for one file — see the module docstring's table."""
    sections = _sections(file_row)
    summary_data = file_row.get('summary_data') or {}

    c_text = ' '.join(sections[:3])
    s0 = sections[0] if sections else ''
    smid = sections[len(sections) // 2] if sections else ''
    slast = sections[-1] if sections else ''

    p_text = app['_summary_highlight_text'](summary_data)
    pi_text = app['structured_summary_text'](summary_data, budget_chars=10**9)

    budget = app['sections_budget'](max(1, n_files_in_scope)) * app['DIGEST_SNIPPET_CHARS']
    remaining = max(0, budget - len(slast))

    p_capped = app['_cut_at_boundary'](p_text, remaining) if len(p_text) > remaining else p_text
    h1 = ' '.join(part for part in (p_capped, slast) if part)

    h2_lead = app['structured_summary_text'](summary_data, budget_chars=remaining)
    h2 = ' '.join(part for part in (h2_lead, slast) if part)

    return {
        'C': c_text,
        'S0': s0,
        'Smid': smid,
        'Slast': slast,
        'P': p_text,
        'PI': pi_text,
        'H1': h1,
        'H2': h2,
    }


def score_question_set(
    questions: list[dict[str, Any]], files: dict[str, dict[str, Any]], app: dict[str, Any]
) -> dict[str, Any]:
    """Score every (question, file, candidate) triple. Returns metrics only — no
    question/reference/answer text, matching this repo's eval-harness convention
    (``tests.eval.harness.probe_metrics.assert_no_prose``'s rule, applied by hand
    here since this script lives outside that package)."""
    from tests.eval.harness import ami_recall

    per_composition: dict[str, dict[str, Any]] = {
        code: {'items_recalled': 0, 'items_total': 0, 'files_covered': set(), 'chars': []}
        for code in CANDIDATE_CODES
    }
    per_shape: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: {
            code: {'items_recalled': 0, 'items_total': 0, 'files_covered': set(), 'chars': []}
            for code in CANDIDATE_CODES
        }
    )

    skipped_no_reference = 0
    skipped_no_file_row = 0
    scored_pairs = 0

    for q in questions:
        shape = q['label'].rsplit('-', 1)[-1]
        reference = q.get('reference') or ''
        if not reference:
            skipped_no_reference += 1
            continue
        tagged = ami_recall.parse_reference_items(reference)
        by_recording: dict[str, list[str]] = defaultdict(list)
        for recording, text in tagged:
            by_recording[recording].append(text)

        for file_uuid in q['file_uuids']:
            file_row = files.get(file_uuid)
            if file_row is None:
                skipped_no_file_row += 1
                continue
            mid = str(file_row['title']).replace(TITLE_PREFIX, '').strip()
            file_reference_lines = by_recording.get(mid)
            if not file_reference_lines:
                continue  # this file's own meeting contributed nothing to this shape's layer
            file_reference = '\n'.join(f'[{mid}] {line}' for line in file_reference_lines)

            candidates = build_candidates(file_row, n_files_in_scope=len(q['file_uuids']), app=app)
            scored_pairs += 1
            for code, text in candidates.items():
                result = ami_recall.score_answer(text, file_reference)
                bucket = per_composition[code]
                shape_bucket = per_shape[shape][code]
                for b in (bucket, shape_bucket):
                    b['items_recalled'] += result.recalled
                    b['items_total'] += result.total
                    b['chars'].append(len(text))
                    if result.recalled > 0:
                        b['files_covered'].add(file_uuid)

    def _summarize(bucket: dict[str, Any], n_files: int) -> dict[str, Any]:
        chars = bucket['chars']
        return {
            'items_recalled': bucket['items_recalled'],
            'items_total': bucket['items_total'],
            'pooled_recall': (
                bucket['items_recalled'] / bucket['items_total'] if bucket['items_total'] else None
            ),
            'files_covered': len(bucket['files_covered']),
            'files_total': n_files,
            'per_file_coverage': (len(bucket['files_covered']) / n_files if n_files else None),
            'mean_chars': statistics.mean(chars) if chars else 0,
            'median_chars': statistics.median(chars) if chars else 0,
        }

    n_files = len({fu for q in questions for fu in q['file_uuids'] if fu in files})
    summary = {
        'by_composition': {
            code: _summarize(per_composition[code], n_files) for code in CANDIDATE_CODES
        },
        'by_shape': {
            shape: {code: _summarize(per_shape[shape][code], n_files) for code in CANDIDATE_CODES}
            for shape in sorted(per_shape)
        },
        'scored_file_question_pairs': scored_pairs,
        'skipped_no_reference': skipped_no_reference,
        'skipped_no_file_row': skipped_no_file_row,
    }
    return summary


def apply_rules(summary: dict[str, Any]) -> dict[str, Any]:
    """Section 4.1's pre-registered rules R-sec, R-items, K0 — computed, not asserted."""
    by_comp = summary['by_composition']

    def _recall(code: str) -> float:
        return by_comp[code]['pooled_recall'] or 0.0

    def _items(code: str) -> int:
        return by_comp[code]['items_recalled']

    slast_recall, slast_items = _recall('Slast'), _items('Slast')
    best_alt_code = max(('S0', 'Smid'), key=_recall)
    best_alt_recall, best_alt_items = _recall(best_alt_code), _items(best_alt_code)

    r_sec_overturned = (
        slast_recall > 0
        and best_alt_recall >= slast_recall * 1.20
        and (best_alt_items - slast_items) >= 5
    )
    r_sec = {
        'chosen_section': best_alt_code if r_sec_overturned else 'Slast',
        'overturned': r_sec_overturned,
        'slast_pooled_recall': slast_recall,
        'slast_items_recalled': slast_items,
        'best_alternative': best_alt_code,
        'best_alternative_pooled_recall': best_alt_recall,
        'best_alternative_items_recalled': best_alt_items,
    }

    pi_items, p_items = _items('PI'), _items('P')
    r_items_ship_pi = (pi_items - p_items) >= 5
    r_items = {
        'ship': 'PI' if r_items_ship_pi else 'P',
        'pi_items_recalled': pi_items,
        'p_items_recalled': p_items,
        'delta_items': pi_items - p_items,
    }

    hybrid_code = 'H2' if r_items_ship_pi else 'H1'
    hybrid_coverage = by_comp[hybrid_code]['per_file_coverage'] or 0.0
    hybrid_recall = by_comp[hybrid_code]['pooled_recall'] or 0.0
    control_coverage = by_comp['C']['per_file_coverage'] or 0.0
    control_recall = by_comp['C']['pooled_recall'] or 0.0
    k0_triggered = (
        control_coverage > 0
        and control_recall > 0
        and hybrid_coverage < 0.75 * control_coverage
        and hybrid_recall < 0.75 * control_recall
    )
    k0 = {
        'chosen_hybrid': hybrid_code,
        'triggered': k0_triggered,
        'hybrid_per_file_coverage': hybrid_coverage,
        'control_per_file_coverage': control_coverage,
        'hybrid_pooled_recall': hybrid_recall,
        'control_pooled_recall': control_recall,
        'coverage_ratio': (hybrid_coverage / control_coverage) if control_coverage else None,
        'recall_ratio': (hybrid_recall / control_recall) if control_recall else None,
    }

    return {'r_sec': r_sec, 'r_items': r_items, 'k0': k0}


def corpus_preconditions(container: str, title_like: str = 'QMSum Product%') -> dict[str, Any]:
    """The two SQL checks Phase 0 step 2 asks for — read-only, reported verbatim."""
    fresh_sql = (
        "SELECT count(*) FILTER (WHERE mf.summary_status='completed' "
        "AND mf.summary_data->'metadata'->>'source_fingerprint' = ff.source_fingerprint) AS fresh, "
        'count(*) AS total FROM media_file mf JOIN file_facts ff ON ff.media_file_id = mf.id '
        f"WHERE mf.title LIKE '{title_like}';"
    )  # noqa: S608
    sections_sql = (
        "SELECT jsonb_array_length(ff.digest->'sections') AS n, count(*) FROM file_facts ff "
        'JOIN media_file mf ON mf.id = ff.media_file_id '
        f"WHERE mf.title LIKE '{title_like}' GROUP BY 1 ORDER BY 1;"
    )  # noqa: S608
    items_sql = (
        "SELECT jsonb_array_length(mf.summary_data->'key_decisions') AS n_dec, "
        "jsonb_array_length(mf.summary_data->'action_items') AS n_act "
        f"FROM media_file mf WHERE mf.title LIKE '{title_like}';"
    )  # noqa: S608

    def _run(sql: str) -> list[str]:
        proc = subprocess.run(  # noqa: S603
            [
                'docker',
                'exec',
                container,
                'psql',
                '-U',
                'postgres',
                '-d',
                'opentranscribe',
                '-tAF',
                '\x1f',
                '-c',
                sql,
            ],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f'precondition query failed: {proc.stderr.strip()}')
        return [line for line in proc.stdout.splitlines() if line.strip()]

    fresh_row = _run(fresh_sql)[0].split('\x1f')
    sections_dist = {row.split('\x1f')[0]: int(row.split('\x1f')[1]) for row in _run(sections_sql)}
    items_rows = [row.split('\x1f') for row in _run(items_sql)]
    dec_dist: dict[str, int] = defaultdict(int)
    act_dist: dict[str, int] = defaultdict(int)
    for n_dec, n_act in items_rows:
        dec_dist[n_dec or '0'] += 1
        act_dist[n_act or '0'] += 1

    return {
        'fresh_summaries': int(fresh_row[0]),
        'total_files': int(fresh_row[1]),
        'gate_p0_pass': int(fresh_row[0]) >= 0.95 * int(fresh_row[1])
        if int(fresh_row[1])
        else False,
        'digest_sections_distribution': sections_dist,
        'key_decisions_count_distribution': dict(dec_dist),
        'action_items_count_distribution': dict(act_dist),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--question-set', type=Path, required=True)
    ap.add_argument('--pg-container', required=True)
    ap.add_argument('--out', type=Path, required=True, help='output dir, metrics-only JSON+MD')
    args = ap.parse_args()

    questions = json.loads(args.question_set.read_text())
    multi_file = [q for q in questions if q['category'] == 'multi_file']
    logger.info('loaded %d multi_file questions from %s', len(multi_file), args.question_set)

    all_uuids = sorted({fu for q in multi_file for fu in q['file_uuids']})
    files = fetch_files(args.pg_container, all_uuids)
    logger.info('fetched %d of %d referenced files', len(files), len(all_uuids))

    app = _load_app_functions()
    summary = score_question_set(multi_file, files, app)
    rules = apply_rules(summary)
    preconditions = corpus_preconditions(args.pg_container)

    results = {
        'schema_version': 1,
        'run_name': 'probe-532-oracle',
        'question_set': str(args.question_set),
        'pg_container': args.pg_container,
        'preconditions': preconditions,
        'summary': summary,
        'rules': rules,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'oracle.json').write_text(json.dumps(results, indent=2, sort_keys=True) + '\n')

    lines = ['# #532 Phase 0 offline content oracle\n']
    lines.append(f'Question set: `{args.question_set}` ({len(multi_file)} multi_file questions)\n')
    lines.append(
        f'Preconditions: fresh={preconditions["fresh_summaries"]}/{preconditions["total_files"]} '
        f'gate_p0_pass={preconditions["gate_p0_pass"]}\n'
    )
    lines.append(
        '\n| composition | items_recalled/total | pooled_recall | per_file_coverage | mean_chars |'
    )
    lines.append('|---|---|---|---|---|')
    for code in CANDIDATE_CODES:
        c = summary['by_composition'][code]
        lines.append(
            f'| {code} | {c["items_recalled"]}/{c["items_total"]} | '
            f'{c["pooled_recall"]:.3f} | {c["per_file_coverage"]:.3f} | {c["mean_chars"]:.0f} |'
            if c['pooled_recall'] is not None and c['per_file_coverage'] is not None
            else f'| {code} | n/a | n/a | n/a | n/a |'
        )
    lines.append('\n## Rules\n')
    lines.append(f'- R-sec: {rules["r_sec"]}')
    lines.append(f'- R-items: {rules["r_items"]}')
    lines.append(f'- K0: {rules["k0"]}')
    (args.out / 'oracle.md').write_text('\n'.join(lines) + '\n')

    logger.info('wrote %s', args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
