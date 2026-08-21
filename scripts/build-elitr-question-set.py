#!/usr/bin/env python3
"""Build the ELITR-Bench probe question set (#521) from the staged NAS copy.

Emits a ``probe_chat_rag.py --question-set`` JSON: one entry per ELITR-Bench QA pair
(271 across 18 meetings), each scoped to its own meeting's injected ``file_uuid`` —
computed here from ``corpus_injection.ids.file_uuid('elitr-bench', meeting_id)``,
which is a pure function of the id, so the set can be built before (or without)
the stack that will answer it. Categories are per question type
(``elitr_who`` / ``elitr_what`` / ``elitr_when`` / ``elitr_howmany``) so the
probe's per-category rollup reports the speaker-attribution axis separately.

Six negative controls are appended (absent speaker / absent topic, scoped to real
meetings, ``expect_refusal: true``) — without them the set cannot distinguish
reading from inventing. Absent topics use invented proper nouns with no
real-world roots, the same convention as the synthetic unanswerable tier.

⚠️ The output embeds ELITR reference prose (transcripts are CC BY-NC-SA-derived,
Tier B) — the writer refuses any ``--out`` outside the gitignored ``.rag-403/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_DATA_DIR = Path('/mnt/nas/opentranscribe-benchmarks')

#: (label suffix, category, question template, meeting index into the sorted id list)
#: Speakers/topics chosen to exist NOWHERE in the corpus: PERSON99 exceeds every real
#: token (max observed PERSON19-ish), and the topics are invented proper nouns.
_NEGATIVE_CONTROLS = [
    ('neg-000-absent-speaker', 'What did [PERSON99] say in this meeting?', 0),
    ('neg-001-absent-topic', 'What was decided about the Zorblatt Industries acquisition?', 3),
    ('neg-002-absent-speaker', "Summarize the Chief Financial Officer's remarks.", 6),
    ('neg-003-absent-topic', 'What was agreed about the Vantorel warehouse lease renewal?', 9),
    ('neg-004-absent-speaker', 'What action items were assigned to the Legal Counsel?', 12),
    ('neg-005-absent-topic', 'What did the group conclude about the Kiruvia data centre?', 15),
]


def build(data_dir: Path, seed: str) -> list[dict[str, Any]]:
    from app.scripts.corpus_injection.ids import file_uuid

    root = data_dir / 'elitr-bench' / 'data'
    entries: list[dict[str, Any]] = []
    meeting_ids: list[str] = []
    for split in ('dev', 'test2'):
        payload = json.loads((root / f'elitr-bench-qa_{split}.json').read_text(encoding='utf-8'))
        for meeting in payload['meetings']:
            meeting_id = meeting['id']
            meeting_ids.append(meeting_id)
            uuid_str = str(file_uuid('elitr-bench', meeting_id, seed))
            for q in meeting['questions']:
                qtype = q['question-type']
                entries.append(
                    {
                        'label': f'elitr-{meeting_id}-q{q["id"]}-{qtype}',
                        'category': f'elitr_{qtype}',
                        'question': q['question'],
                        'file_uuids': [uuid_str],
                        'scope_desc': meeting_id,
                        'reference': q['groundtruth-answer'],
                        'answer_position': q['answer-position'],
                    }
                )
    meeting_ids.sort()
    for label, question, idx in _NEGATIVE_CONTROLS:
        meeting_id = meeting_ids[idx % len(meeting_ids)]
        entries.append(
            {
                'label': f'elitr-{label}',
                'category': 'negative_control',
                'question': question,
                'file_uuids': [str(file_uuid('elitr-bench', meeting_id, seed))],
                'scope_desc': meeting_id,
                'expect_refusal': True,
            }
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--seed', default='', help='corpus_injection namespace seed')
    parser.add_argument(
        '--out', type=Path, default=REPO / '.rag-403' / 'probe-runs' / 'question-set-elitr.json'
    )
    args = parser.parse_args(argv)

    if '.rag-403' not in args.out.resolve().parts:
        raise SystemExit(
            f'{args.out}: the set embeds Tier-B reference prose and must live under '
            '.rag-403/ (gitignored)'
        )
    entries = build(args.data_dir, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    from collections import Counter

    counts = Counter(e['category'] for e in entries)
    print(f'{len(entries)} questions -> {args.out}')
    print(dict(sorted(counts.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
