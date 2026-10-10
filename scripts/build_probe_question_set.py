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

``--corpus-scale`` (issue #829, opt-in — off unless the flag is passed)
    Every stratum above scopes ``file_uuids`` to the one meeting (or series) a
    question is actually about — at most 4 files. That structurally cannot catch a
    RANKING-VS-MAPPING divergence (see ``rag-evaluation.md``'s "AMI distractor
    haystack": ``build_overview`` once composed itself from the ranked
    ``retrieve_digests`` leg instead of the mapping ``scope_digest_hits`` leg and
    returned 50 sections from only 8 of a 25-file scope), because at scope size <=4
    there is nothing to rank AWAY from. This stratum reuses the SAME question shapes
    (multi-file series questions, real QMSum needle questions, a scope-wide broad
    aggregation prompt, a negative control) but scopes every one of them to the full
    corpus actually injected this run: the QMSum meetings indexed for the strata
    above, UNION the 34-meeting AMI distractor haystack
    (``app/scripts/corpus_injection/adapters/ami.py``, injected separately via
    ``./scripts/inject-eval-corpus.sh --corpus ami``). See :func:`build_corpus_scale`.

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

#: Title prefix ``adapters/ami.py`` writes for the 34-meeting distractor haystack (issue
#: #461 A5) — see that module's docstring. Same corpus injector, same production
#: indexing path as QMSum's own ``TITLE_PREFIX``; used only to widen a question's
#: ``file_uuids`` scope, never to source questions of its own (the distractor set ships
#: no relevance judgements — see ``rag-evaluation.md``'s "AMI distractor haystack").
AMI_DISTRACTOR_TITLE_PREFIX = 'AMI (distractor) — '

#: Shape templates for a cross-meeting question over a real AMI scenario series, paired
#: with the AMI abstractive layer that answers it. Hoisted to module scope (was local to
#: ``build()``) so :func:`build_corpus_scale` can reuse the exact same shapes at full-
#: corpus scope — the point of that stratum is to ask the SAME question AMI-81 already
#: asks, just against a much bigger haystack, so any divergence is attributable to scope
#: alone.
MULTI_FILE_SHAPES = (
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

#: Broad, scope-wide prompts with no single-series anchor — the shape that actually
#: reproduces the ranking-vs-mapping divergence documented in ``rag-evaluation.md``
#: ("AMI distractor haystack" / the ``build_overview`` ``retrieve_digests`` vs
#: ``scope_digest_hits`` bug): a summarization request over the WHOLE scope, where the
#: correct behaviour is to consult every file the scope names, not just the top-ranked
#: handful. No reference answer exists for these (there is no single human-written
#: summary of an arbitrary cross-series AMI selection) — they are scored on
#: ``coverage_ratio``/``files_consulted`` from ``--metrics-out``, not on answer text.
CORPUS_SCALE_BROAD_PROMPTS = (
    'what topics were discussed across all the meetings in this scope?',
    'summarise the main decisions made across every meeting in this scope.',
)


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


def find_series(meeting_ids: list[str], min_size: int = 3) -> dict[str, list[str]]:
    """Group AMI-style meeting ids (``ES2002a`` -> series ``ES2002``) with >= ``min_size``
    sessions present. Shared by :func:`build` and :func:`build_corpus_scale` so the two
    strata cannot silently diverge on what counts as a "real" series.
    """
    series: dict[str, list[str]] = defaultdict(list)
    for mid in meeting_ids:
        m = SERIES_RE.match(mid)
        if m:
            series[m.group(1)].append(mid)
    return {s: sorted(mids) for s, mids in series.items() if len(mids) >= min_size}


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
    series = find_series(usable, min_size=3)
    full = sorted(series)
    rng.shuffle(full)

    # Each shape is paired with the AMI layer that ANSWERS it, so a cross-meeting
    # question carries a real human-written reference rather than None.
    grounded = 0
    for i, s in enumerate(full[:per_stratum]):
        mids = series[s]
        tmpl, kind, layer = MULTI_FILE_SHAPES[i % len(MULTI_FILE_SHAPES)]
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


def build_multi_file_expanded(
    qmsum: dict[str, dict[str, Any]],
    indexed: dict[str, str],
    ami: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """#532 follow-up (Unit U8, ``docs/design/532_hybrid_summary_synthesis_plan.md``): every
    grounded ``multi_file`` question the corpus can produce, not a random ``per_stratum``
    sample of them.

    :func:`build`'s ``multi_file`` stratum takes ``per_stratum`` series at random and pairs
    each with exactly ONE shape (round-robin over :data:`MULTI_FILE_SHAPES`) — AMI-81's
    n=25 was sized for a quick smoke run, not for detecting a ~7-point effect (the plan's
    section 4.2: SDs measured in F1 give a 95% CI half-width of ±0.073 on USED coverage at
    n≈130, vs ±0.167 at n=25). This emits **every** (series, shape) pair instead: for every
    series with >= 3 usable sessions, all four :data:`MULTI_FILE_SHAPES`.

    Only GROUNDED entries survive — :func:`series_reference` returning ``None`` for a
    (series, shape) pair is dropped rather than emitted with ``reference: null``, because an
    ungrounded multi-file question cannot be scored on content coverage (M2/M3 in the plan),
    only on USED coverage (M1) — and the plan's decision set needs both.

    Labels are ``multi-<series>-<kind>`` — stable and SEED-INDEPENDENT (unlike ``build()``'s
    ``multi-{i:03d}-{s}-{kind}``, whose index depends on shuffle order), so the same series+
    shape pair gets the same label across reruns and across a comparison against AMI-81's own
    ``multi_file`` labels (see :func:`series_reference` above and
    ``tests/eval/test_build_probe_question_set.py``'s superset test).

    Args:
        qmsum: Every loaded QMSum Product meeting, keyed by meeting id.
        indexed: QMSum meetings actually injected this run -> file_uuid.
        ami: AMI abstractive layers, as loaded by :func:`load_ami_abstractive`.

    Returns:
        One ``multi_file`` entry per grounded (series, shape) pair. Deterministic — no
        ``random.Random`` involved, unlike every other stratum here.
    """
    usable = sorted(set(qmsum) & set(indexed))
    series = find_series(usable, min_size=3)

    out: list[dict[str, Any]] = []
    for s in sorted(series):
        mids = series[s]
        for tmpl, kind, layer in MULTI_FILE_SHAPES:
            ref = series_reference(ami, mids, layer)
            if not ref:
                continue
            out.append(
                {
                    'label': f'multi-{s}-{kind}',
                    'category': 'multi_file',
                    'question': tmpl.format(n=len(mids), s=s),
                    'file_uuids': [indexed[m] for m in mids],
                    'scope_desc': f'{s} series ({", ".join(mids)})',
                    'reference': ref,
                    'reference_source': f'AMI abstractive <{layer}>, unioned across the series',
                }
            )
    logger.info(
        'multi_file_expanded: %d series, %d grounded (series, shape) pairs of a possible %d',
        len(series),
        len(out),
        len(series) * len(MULTI_FILE_SHAPES),
    )
    return out


def build_corpus_scale(
    qmsum: dict[str, dict[str, Any]],
    indexed: dict[str, str],
    distractor_indexed: dict[str, str],
    ami: dict[str, dict[str, list[str]]],
    seed: int,
    n_needle: int = 3,
    n_broad: int = 2,
) -> list[dict[str, Any]]:
    """Widen a handful of AMI-81-shaped questions to FULL-CORPUS scope (issue #829).

    Every other stratum in :func:`build` scopes ``file_uuids`` to the one meeting (or
    one series) a question is actually about — which structurally cannot catch a
    ranking-vs-mapping divergence, because at scope size 1-4 there is nothing to rank
    AWAY from. This stratum takes the same question shapes and scopes them to the
    UNION of every meeting injected for this run — the QMSum meetings actually indexed
    (``indexed``) plus the full AMI distractor haystack (``distractor_indexed``,
    ``adapters/ami.py`` — see its module docstring and ``rag-evaluation.md``'s "AMI
    distractor haystack" section) — so the correct answer is still findable, but now
    has to be FOUND and MAPPED across everything else in the scope, not merely
    returned as the sole candidate.

    Three question shapes, same reasoning `rag-evaluation.md`'s already-reproduced bug
    (`build_overview` composed from the ranked ``retrieve_digests`` leg instead of the
    mapping ``scope_digest_hits`` leg — 50 sections drawn from 8 of a 25-file scope)
    calls for:

    * ``multi_file_corpus_scale`` — the SAME series questions :func:`build` asks at
      series-only scope (4 files), reusing :data:`MULTI_FILE_SHAPES` and the AMI
      abstractive reference, but scoped to the full corpus. Directly comparable to the
      series-scope version: same question, same reference, only the haystack changed.
    * ``single_specific_corpus_scale`` / ``single_general_corpus_scale`` — real QMSum
      questions ("needle" tests) at full-corpus scope, checking whether chunk-tier
      retrieval still discriminates the correct meeting from 30+ distractors.
    * ``corpus_scale_broad`` — :data:`CORPUS_SCALE_BROAD_PROMPTS`, scope-wide
      aggregation questions with no single-series anchor and no reference answer
      (scored on ``coverage_ratio``/``files_consulted`` from ``--metrics-out``, not
      answer text) — the shape that most directly exercises the map-vs-rank divergence.

    Plus one ``negative_control_corpus_scale`` (an absent topic, full scope) to check
    the model still declines correctly rather than latching onto an unrelated
    distractor meeting.

    Args:
        qmsum: Every loaded QMSum Product meeting, keyed by meeting id.
        indexed: QMSum meetings actually injected this run -> file_uuid.
        distractor_indexed: AMI distractor meetings actually injected this run ->
            file_uuid (``indexed_meetings(container, prefix=AMI_DISTRACTOR_TITLE_PREFIX)``).
        ami: AMI abstractive layers, as loaded by :func:`load_ami_abstractive`.
        seed: Shared with :func:`build` for reproducibility, not required to match it.
        n_needle: Specific+general "needle" questions to build (each, not combined).
        n_broad: Broad aggregation questions to build, capped at
            ``len(CORPUS_SCALE_BROAD_PROMPTS)``.

    Returns:
        The corpus-scale question list. Empty (with a logged warning), never a raised
        error, when no distractor meetings are indexed — a corpus-scale run with zero
        distractors would silently degrade to the single_file case this stratum exists
        to distinguish itself from.
    """
    if not distractor_indexed:
        logger.warning(
            'build_corpus_scale: distractor_indexed is empty — inject the "ami" corpus '
            'first (adapters/ami.py, prefix %r). Returning no corpus-scale questions.',
            AMI_DISTRACTOR_TITLE_PREFIX,
        )
        return []

    rng = random.Random(seed)
    usable = sorted(set(qmsum) & set(indexed))
    full_scope = sorted({indexed[m] for m in usable} | set(distractor_indexed.values()))
    logger.info(
        'corpus_scale: qmsum_usable=%d distractors=%d full_scope=%d',
        len(usable),
        len(distractor_indexed),
        len(full_scope),
    )

    out: list[dict[str, Any]] = []

    # --- multi_file_corpus_scale: same series shapes as `build()`, full-corpus scope.
    series = find_series(usable, min_size=3)
    series_ids = sorted(series)
    for i, s in enumerate(series_ids):
        mids = series[s]
        tmpl, kind, layer = MULTI_FILE_SHAPES[i % len(MULTI_FILE_SHAPES)]
        ref = series_reference(ami, mids, layer)
        out.append(
            {
                'label': f'scale-{i:03d}-{s}-{kind}',
                'category': 'multi_file_corpus_scale',
                'question': tmpl.format(n=len(mids), s=s),
                'file_uuids': full_scope,
                'scope_desc': f'{s} series ({", ".join(mids)}) + {len(full_scope) - len(mids)} '
                'other corpus files',
                'reference': ref,
                'reference_source': f'AMI abstractive <{layer}>, unioned across the series'
                if ref
                else None,
            }
        )
    n = len(series_ids)

    # --- needle tests: real QMSum specific/general questions, full-corpus scope.
    series_members = {m for mids in series.values() for m in mids}
    needle_pool = [m for m in usable if m not in series_members] or usable
    specific: list[tuple[str, dict[str, Any]]] = []
    general: list[tuple[str, dict[str, Any]]] = []
    for mid in needle_pool:
        for q in qmsum[mid].get('specific_query_list') or []:
            specific.append((mid, q))
        for q in qmsum[mid].get('general_query_list') or []:
            general.append((mid, q))

    for i, (mid, q) in enumerate(rng.sample(specific, min(n_needle, len(specific)))):
        out.append(
            {
                'label': f'scale-{n + i:03d}-{mid}-needle-specific',
                'category': 'single_specific_corpus_scale',
                'question': q['query'],
                'file_uuids': full_scope,
                'scope_desc': f'{mid} + {len(full_scope) - 1} other corpus files',
                'reference': q.get('answer'),
            }
        )
    n += min(n_needle, len(specific))

    for i, (mid, q) in enumerate(rng.sample(general, min(n_needle, len(general)))):
        out.append(
            {
                'label': f'scale-{n + i:03d}-{mid}-needle-general',
                'category': 'single_general_corpus_scale',
                'question': q['query'],
                'file_uuids': full_scope,
                'scope_desc': f'{mid} + {len(full_scope) - 1} other corpus files',
                'reference': q.get('answer'),
            }
        )
    n += min(n_needle, len(general))

    # --- broad aggregation: no reference, the shape that most directly reproduces the
    # documented ranking-vs-mapping divergence.
    for i, prompt in enumerate(CORPUS_SCALE_BROAD_PROMPTS[: max(0, n_broad)]):
        out.append(
            {
                'label': f'scale-{n + i:03d}-broad',
                'category': 'corpus_scale_broad',
                'question': prompt,
                'file_uuids': full_scope,
                'scope_desc': f'full corpus ({len(full_scope)} files)',
                'reference': None,
            }
        )
    n += min(n_broad, len(CORPUS_SCALE_BROAD_PROMPTS))

    # --- negative control at full scope.
    out.append(
        {
            'label': f'scale-{n:03d}-absent-topic',
            'category': 'negative_control_corpus_scale',
            'question': f'what was decided about {ABSENT_TOPICS[0]}?',
            'file_uuids': full_scope,
            'scope_desc': f'full corpus ({len(full_scope)} files)',
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
    ap.add_argument(
        '--corpus-scale',
        action='store_true',
        help='Also append a corpus-scale stratum (issue #829): widens a handful of '
        'the same question shapes to full-corpus scope (QMSum meetings actually '
        'indexed + the injected AMI distractor haystack), to catch a ranking-vs-'
        'mapping divergence that single-file/single-series scope cannot. Requires '
        'the "ami" corpus already injected alongside qmsum '
        '(./scripts/inject-eval-corpus.sh --corpus ami).',
    )
    ap.add_argument(
        '--distractor-prefix',
        default=AMI_DISTRACTOR_TITLE_PREFIX,
        help='Title prefix the AMI distractor adapter wrote (only used with --corpus-scale)',
    )
    ap.add_argument(
        '--corpus-scale-needle',
        type=int,
        default=3,
        help='--corpus-scale: specific+general "needle" questions to build (each)',
    )
    ap.add_argument(
        '--corpus-scale-broad',
        type=int,
        default=2,
        help='--corpus-scale: scope-wide aggregation questions with no reference',
    )
    ap.add_argument(
        '--multi-file-expanded',
        action='store_true',
        help='#532 follow-up: build a SEPARATE question set containing ONLY multi_file '
        'entries, for every grounded (series, shape) pair the corpus can produce (~34 '
        "series x 4 shapes) rather than build()'s per_stratum-sized random sample. "
        'Mutually exclusive with the default strata in one invocation — run this as its '
        'own --out file (the AMI-81 set stays a separate, untouched build).',
    )
    args = ap.parse_args()

    if '.rag-403' not in str(args.out) and '/tmp' not in str(args.out):
        logger.warning(
            '⚠️  %s is not under .rag-403/ or /tmp — the output embeds QMSum question and '
            'reference text verbatim and MUST NOT be committed to this public repo.',
            args.out,
        )

    qmsum = load_qmsum(args.qmsum_glob)
    indexed = indexed_meetings(args.pg_container)
    ami = load_ami_abstractive(args.ami_glob)

    if args.multi_file_expanded:
        qs = build_multi_file_expanded(qmsum, indexed, ami)
    else:
        qs = build(qmsum, indexed, ami, args.per_stratum, args.seed)
        if args.corpus_scale:
            distractor_indexed = indexed_meetings(args.pg_container, prefix=args.distractor_prefix)
            qs += build_corpus_scale(
                qmsum,
                indexed,
                distractor_indexed,
                ami,
                args.seed,
                args.corpus_scale_needle,
                args.corpus_scale_broad,
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
