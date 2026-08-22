#!/usr/bin/env python3
"""Build a stratified ``--question-set`` for :mod:`scripts.probe_chat_rag` from QMSum.

QMSum's **Product** split *is* AMI's scenario corpus (a design team building a TV
remote across four meetings), and it ships **human-written** queries with
**human-written reference answers** — 1,972 pairs across the 137 meetings indexed
here. The live probe was using 14 of them, which is too few to settle anything: a
four-question multi-file sample could not distinguish a real coverage change from
noise.

⚠️ **This script is committable; its OUTPUT is not.** The generated question set
embeds QMSum question and reference text verbatim, and this repo is public while
`docs-site/docs/developer-guide/rag-evaluation.md` records QMSum's licence as
"MIT but the README asks research-only use — AMBIGUOUS". Write the output under
`.rag-403/` (gitignored), exactly as `probe_chat_rag.py --question-set` expects.
The same split is why `probe_chat_rag.py` takes its questions at runtime instead
of hardcoding them.

Stratification, and why each stratum earns its place:

``single_specific``
    A targeted question against its own meeting. The base case — if this fails,
    nothing else is meaningful.
``single_general``
    "Summarise the whole meeting", which routes through the digest/overview tier
    rather than the chunk tier. Kept as a separate stratum because those are
    different code paths and a change can move one without the other — the
    digest-plane crash of 2026-08-20 hit ONLY this stratum and read as a coverage
    regression until the strata were separated.
``multi_file``
    Constructed across a meeting *series* (ES2002a-d, TS3005a-d, …), because
    QMSum has no cross-meeting questions. This is the stratum that exercises
    scope coverage, and the one where "find the due-outs from all these meetings"
    lives.
``negative_control``
    A speaker or topic genuinely absent from the selection. ⚠️ **Not optional.**
    Without it the suite cannot distinguish reading from inventing, and a system
    that confidently answers anything scores identically to one that reads
    correctly.

Usage::

    python3 scripts/build_probe_question_set.py \\
        --out .rag-403/probe-runs/question-set-large.json --per-stratum 25
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger('build_question_set')

QMSUM_GLOB = '/mnt/nas/opentranscribe-benchmarks/qmsum/QMSum-*/data/Product/*/*.json'
TITLE_PREFIX = 'QMSum Product — '

#: A meeting id like ``ES2002a`` -> series ``ES2002``, session ``a``. AMI's scenario
#: meetings come in four-session series, which is what makes a *constructed*
#: multi-file question meaningful rather than an arbitrary grouping of recordings.
SERIES_RE = re.compile(r'^([A-Z]{2}\d{4})([a-z])$')

#: AMI's manual abstractive annotations, which carry HUMAN-WRITTEN reference text in
#: four layers per meeting: ``abstract``, ``actions``, ``decisions``, ``problems``.
#: All four are present in every file (verified across the corpus), and they map 1:1
#: onto the multi-file question shapes below — so a cross-meeting question gets a real
#: reference answer instead of ``None``.
#:
#: ⚠️ This is what makes the "find the due-outs from all these meetings" question
#: *measurable*: ``<actions>`` IS the due-outs ground truth, written by an annotator
#: rather than inferred by us. QMSum has no cross-meeting references at all.
AMI_ABSTRACTIVE_GLOB = (
    '/mnt/nas/opentranscribe-benchmarks/ami/ami_public_manual_1.6.2/abstractive/*.abssumm.xml'
)

#: Layer text is ISO-8859-1, not UTF-8 — the AMI XML declares it and it genuinely
#: contains bytes that are not valid UTF-8. Decoding as UTF-8 raises on real files.
AMI_ENCODING = 'ISO-8859-1'

_LAYER_RE = {
    layer: re.compile(rf'<{layer}\b[^>]*>(.*?)</{layer}>', re.DOTALL)
    for layer in ('abstract', 'actions', 'decisions', 'problems')
}
_SENTENCE_RE = re.compile(r'<sentence[^>]*>(.*?)</sentence>', re.DOTALL)

#: Topics and roles that appear in NO AMI scenario meeting. Used for the negative
#: controls. Deliberately plausible-sounding for a corporate meeting corpus — a
#: control the model can dismiss on vocabulary alone tests nothing.
ABSENT_TOPICS = (
    'the quarterly cloud infrastructure migration budget',
    'the results of the phase III clinical trial',
    'the merger with the Helsinki subsidiary',
    'the penetration test findings for the payment gateway',
)
ABSENT_SPEAKERS = ('the Chief Financial Officer', 'the Legal Counsel', 'the Head of Procurement')


def indexed_meetings(container: str, prefix: str = TITLE_PREFIX) -> dict[str, str]:
    """Map QMSum meeting id -> indexed ``file_uuid``, read from Postgres.

    Args:
        container: Postgres container name, e.g. ``otfresh-ragmeas-postgres``.
        prefix: Title prefix the corpus injector wrote.

    Returns:
        ``{"TS3005d": "<uuid>", ...}`` for meetings that are actually indexed.

    Raises:
        SystemExit: The query failed — better to stop than to build a question set
            against meetings that may not exist.
    """
    sql = f"SELECT uuid, title FROM media_file WHERE title LIKE '{prefix}%';"  # noqa: S608
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
            '-tAc',
            sql,
        ],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f'could not read indexed meetings from {container}: {proc.stderr.strip()}')
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if '|' not in line:
            continue
        uuid, title = line.split('|', 1)
        out[title.replace(prefix, '').strip()] = uuid.strip()
    return out


def load_ami_abstractive(
    glob_pattern: str = AMI_ABSTRACTIVE_GLOB,
) -> dict[str, dict[str, list[str]]]:
    """Load AMI's manual abstractive layers, keyed by meeting id.

    Returns:
        ``{"ES2002a": {"abstract": [...], "actions": [...], "decisions": [...],
        "problems": [...]}, ...}`` — each a list of human-written sentences.

    Parsed with regex rather than an XML parser on purpose: the NITE format nests
    ``nite:child`` href pointers we do not want, the files are ISO-8859-1, and we need
    exactly four flat sentence lists. A parser buys nothing here and adds a dependency.
    """
    import glob as _glob

    out: dict[str, dict[str, list[str]]] = {}
    for path in sorted(_glob.glob(glob_pattern)):
        mid = Path(path).name.split('.')[0]
        try:
            raw = Path(path).read_text(encoding=AMI_ENCODING)
        except (OSError, UnicodeDecodeError) as exc:  # a corpus file we cannot read is not fatal
            logger.warning('skipping AMI abstractive %s: %s', path, exc)
            continue
        layers: dict[str, list[str]] = {}
        for layer, rx in _LAYER_RE.items():
            m = rx.search(raw)
            if not m:
                continue
            sentences = [' '.join(s.split()) for s in _SENTENCE_RE.findall(m.group(1)) if s.strip()]
            if sentences:
                layers[layer] = sentences
        if layers:
            out[mid] = layers
    return out


def series_reference(
    ami: dict[str, dict[str, list[str]]], meeting_ids: list[str], layer: str
) -> str | None:
    """Union one AMI layer across a meeting series into a single reference answer.

    A cross-meeting question ("what were the decisions across all four?") has no single
    annotated answer, but the union of each session's annotated layer IS the answer a
    human would give. Sentences are prefixed with their meeting id so a judge — human or
    model — can see which session each item came from, which is exactly the attribution
    a coverage failure destroys.

    Returns:
        The joined reference, or ``None`` when no session in the series carries that
        layer — in which case the question is emitted WITHOUT a reference rather than
        with a fabricated one.
    """
    lines: list[str] = []
    for mid in meeting_ids:
        for sentence in (ami.get(mid) or {}).get(layer, []):
            lines.append(f'[{mid}] {sentence}')
    return '\n'.join(lines) if lines else None


def load_qmsum(glob_pattern: str) -> dict[str, dict[str, Any]]:
    """Load every QMSum Product file, keyed by meeting id (deduped across splits)."""
    import glob as _glob

    by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(_glob.glob(glob_pattern)):
        mid = Path(path).stem
        if mid in by_id:  # the same meeting appears under all/ and its split dir
            continue
        by_id[mid] = json.load(open(path))  # noqa: SIM115
    return by_id


def build(
    qmsum: dict[str, dict[str, Any]],
    indexed: dict[str, str],
    ami: dict[str, dict[str, list[str]]],
    per_stratum: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Assemble the stratified question set.

    Only meetings that are BOTH in QMSum and indexed are used — a question about a
    meeting that was never injected measures the injector, not retrieval.
    """
    rng = random.Random(seed)
    usable = sorted(set(qmsum) & set(indexed))
    logger.info('qmsum=%d indexed=%d usable=%d', len(qmsum), len(indexed), len(usable))

    out: list[dict[str, Any]] = []

    specific: list[tuple[str, dict[str, Any]]] = []
    general: list[tuple[str, dict[str, Any]]] = []
    for mid in usable:
        for q in qmsum[mid].get('specific_query_list') or []:
            specific.append((mid, q))
        for q in qmsum[mid].get('general_query_list') or []:
            general.append((mid, q))

    for i, (mid, q) in enumerate(rng.sample(specific, min(per_stratum, len(specific)))):
        out.append(
            {
                'label': f'spec-{i:03d}-{mid}',
                'category': 'single_specific',
                'question': q['query'],
                'file_uuids': [indexed[mid]],
                'scope_desc': mid,
                'reference': q.get('answer'),
            }
        )

    for i, (mid, q) in enumerate(rng.sample(general, min(per_stratum, len(general)))):
        out.append(
            {
                'label': f'gen-{i:03d}-{mid}',
                'category': 'single_general',
                'question': q['query'],
                'file_uuids': [indexed[mid]],
                'scope_desc': mid,
                'reference': q.get('answer'),
            }
        )

    # Multi-file: whole series, so "across all these meetings" is a real scope.
    series: dict[str, list[str]] = defaultdict(list)
    for mid in usable:
        m = SERIES_RE.match(mid)
        if m:
            series[m.group(1)].append(mid)
    full = sorted(s for s, mids in series.items() if len(mids) >= 3)
    rng.shuffle(full)

    # Each shape is paired with the AMI layer that ANSWERS it, so a cross-meeting
    # question carries a real human-written reference rather than None.
    shapes = (
        ('what were the key decisions made across all {n} {s} meetings?', 'decisions', 'decisions'),
        (
            'what action items or follow-ups came out of the {s} meeting series?',
            'action_items',
            'actions',
        ),
        ('what problems or concerns were raised across the {s} meetings?', 'problems', 'problems'),
        (
            'summarise how the design evolved across the {s} meeting series.',
            'evolution',
            'abstract',
        ),
    )
    grounded = 0
    for i, s in enumerate(full[:per_stratum]):
        mids = sorted(series[s])
        tmpl, kind, layer = shapes[i % len(shapes)]
        ref = series_reference(ami, mids, layer)
        grounded += bool(ref)
        out.append(
            {
                'label': f'multi-{i:03d}-{s}-{kind}',
                'category': 'multi_file',
                'question': tmpl.format(n=len(mids), s=s),
                'file_uuids': [indexed[m] for m in mids],
                'scope_desc': f'{s} series ({", ".join(mids)})',
                'reference': ref,
                'reference_source': f'AMI abstractive <{layer}>, unioned across the series'
                if ref
                else None,
            }
        )
    logger.info(
        'multi_file: %d/%d grounded in AMI annotations', grounded, min(len(full), per_stratum)
    )

    # Negative controls — absent topic and absent speaker, over a real scope.
    n_neg = max(4, per_stratum // 4)
    for i in range(n_neg):
        s = full[i % max(1, len(full))]
        mids = sorted(series[s])
        uuids = [indexed[m] for m in mids]
        if i % 2 == 0:
            q = f'what was decided about {ABSENT_TOPICS[i % len(ABSENT_TOPICS)]}?'
            label = f'neg-{i:03d}-absent-topic'
        else:
            q = f'what did {ABSENT_SPEAKERS[i % len(ABSENT_SPEAKERS)]} say in these meetings?'
            label = f'neg-{i:03d}-absent-speaker'
        out.append(
            {
                'label': label,
                'category': 'negative_control',
                'question': q,
                'file_uuids': uuids,
                'scope_desc': f'{s} series',
                'reference': None,
                'expect_refusal': True,
            }
        )

    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--out', type=Path, required=True, help='output JSON (put it under .rag-403/)')
    ap.add_argument('--per-stratum', type=int, default=25)
    ap.add_argument('--seed', type=int, default=20260820, help='fixed so the set is reproducible')
    ap.add_argument('--pg-container', default='otfresh-ragmeas-postgres')
    ap.add_argument('--qmsum-glob', default=QMSUM_GLOB)
    ap.add_argument('--ami-glob', default=AMI_ABSTRACTIVE_GLOB)
    args = ap.parse_args()

    if '.rag-403' not in str(args.out) and '/tmp' not in str(args.out):
        logger.warning(
            '⚠️  %s is not under .rag-403/ or /tmp — the output embeds QMSum question and '
            'reference text verbatim and MUST NOT be committed to this public repo.',
            args.out,
        )

    qs = build(
        load_qmsum(args.qmsum_glob),
        indexed_meetings(args.pg_container),
        load_ami_abstractive(args.ami_glob),
        args.per_stratum,
        args.seed,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(qs, indent=2))

    counts: dict[str, int] = defaultdict(int)
    for q in qs:
        counts[q['category']] += 1
    logger.info('wrote %s (%d questions)', args.out, len(qs))
    for k in sorted(counts):
        logger.info('  %-18s %d', k, counts[k])
    return 0


if __name__ == '__main__':
    sys.exit(main())
