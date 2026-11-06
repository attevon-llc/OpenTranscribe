"""Inputs: which meetings, how they are rendered into the app's transcript format, how they slice.

All of it is Tier A (AMI and ICSI are CC BY 4.0, QMSum's annotations are MIT). Corpus text is
rendered in memory for a run and never written into the repo; only hashes, lengths and counts
reach the lock file.

The decision to summarize the human reference transcript (not ASR output) in-process is plan
section 3.1: the variable under test is prompt wording, gold alignment needs the reference, arm
injection needs one process, and the slicing is still the app's own chunker.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from summary_bench.config import CONTEXT_CEILING, ExitCode, HarnessError, ensure_app_environment

CONCAT_GAP_S = 30.0
TAIL_MIN_CHARS, TAIL_MAX_CHARS, TAIL_TARGET_CHARS = 200, 600, 386
TAIL_SEARCH = range(12_000, 40_001, 50)
LONG_FLOOR_TOKENS = 16_000
LONG_CHUNKS = (4, 5)
_STAMP = re.compile(r'\[(\d+):(\d{2})\]')

KIND_TAIL = 'tail'  # a ~1 h meeting that ends in a tiny crosstalk slice (criteria a, b, c)
KIND_LONG = 'long'  # a >= 2 h recording in 4 or 5 slices (criteria a, b)
KIND_FAILURE = 'failure'  # the long recording with one slice forced to fail (criterion d)


@dataclass(frozen=True)
class InputSpec:
    """One input of the run matrix."""

    input_id: str
    meetings: tuple[str, ...]
    kind: str
    #: How the failing slice is made to fail ("exception" or "parse"); None for no failure.
    failure_mode: str | None = None
    #: Another input whose rendering and locked slicing this one shares (I4 reuses I3's).
    shares_with: str | None = None


GATE_INPUTS = (
    InputSpec('I1', ('Bed003',), KIND_TAIL),
    InputSpec('I2', ('Bmr023',), KIND_TAIL),
    InputSpec('I3', ('ES2008a', 'ES2008b', 'ES2008c', 'ES2008d'), KIND_LONG),
    InputSpec('I4', ('ES2008a', 'ES2008b', 'ES2008c', 'ES2008d'), KIND_FAILURE, 'exception', 'I3'),
    InputSpec('I4p', ('ES2008a', 'ES2008b', 'ES2008c', 'ES2008d'), KIND_FAILURE, 'parse', 'I3'),
)
# Development inputs: the new rules are tuned on these, never on the gate inputs. The plan named
# Bed010 and Bmr012; Bed010 has no max_tokens that leaves a 200-600 char tail free of gold items
# (plan-slices refuses it), so D1 is Bed005, the nearest qualifying ICSI meeting to the 386 char
# target outside the gate set.
DEV_INPUTS = (
    InputSpec('D1', ('Bed005',), KIND_TAIL),
    InputSpec('D2', ('Bmr012',), KIND_TAIL),
    InputSpec('D3', ('IS1003a', 'IS1003b', 'IS1003c', 'IS1003d'), KIND_LONG),
)
SMOKE_INPUTS = (GATE_INPUTS[0],)
INPUT_SETS: dict[str, tuple[InputSpec, ...]] = {
    'gate': GATE_INPUTS,
    'dev': DEV_INPUTS,
    'smoke': SMOKE_INPUTS,
}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------- corpus layout


@dataclass(frozen=True)
class CorpusPaths:
    """Where the three corpora live under the data directory."""

    qmsum: Path
    ami: Path
    icsi: Path
    ami_manual: Path
    icsi_plus: Path

    @classmethod
    def under(cls, data_dir: Path) -> CorpusPaths:
        ami, icsi = data_dir / 'ami', data_dir / 'icsi'
        manual = _nested(ami, 'abstractive')
        plus = _nested(icsi, 'ICSIplus')
        missing = [
            str(p) for p in (data_dir / 'qmsum', manual, plus) if p is None or not Path(p).exists()
        ]
        if missing or manual is None or plus is None:
            raise HarnessError(
                f'corpus not found under {data_dir} (looked for qmsum, ami, icsi): {missing}',
                ExitCode.PRECONDITION,
            )
        return cls(data_dir / 'qmsum', ami, icsi, manual.parent, plus.parent)


def _nested(base: Path, marker: str) -> Path | None:
    """``base`` or one of its children, whichever holds ``marker``; returns the marker path."""
    if (base / marker).exists():
        return base / marker
    for child in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        if (child / marker).exists():
            return child / marker
    return None


# ---------------------------------------------------------------------------- rendering


@dataclass
class _Speaker:
    """Stand-in for a diarized `Speaker` row: the raw label, no display or suggested name."""

    name: str
    display_name: str | None = None
    suggested_name: str | None = None


@dataclass
class _Segment:
    """Satisfies `MaskableSegment` (transcript_builders.py)."""

    text: str
    start_time: float
    end_time: float
    speaker: _Speaker
    redactions: list[Any] | None = None
    words: list[Any] | None = None


@dataclass
class RenderedInput:
    """A rendered transcript plus the facts the planner needs. Holds corpus text: memory only."""

    transcript: str
    speaker_data: dict[str, Any]
    #: Seconds added to each meeting's times when concatenating (all 0.0 for a single meeting).
    offsets: dict[str, float] = field(default_factory=dict)


def _ami_identities(paths: CorpusPaths, meeting_id: str) -> dict[str, str]:
    """QMSum role label -> AMI global participant id, so one person keeps one label across a series."""
    from app.scripts.corpus_injection.adapters.qmsum import AMI_ROLE_TO_QMSUM
    from app.scripts.corpus_injection.nxt import read_meeting_channels

    meetings_xml = paths.ami_manual / 'corpusResources' / 'meetings.xml'
    channels = read_meeting_channels(meetings_xml).get(meeting_id, {})
    return {
        AMI_ROLE_TO_QMSUM[info['role']]: info['global_name']
        for info in channels.values()
        if info['role'] in AMI_ROLE_TO_QMSUM and info['global_name']
    }


def load_timed_turns(paths: CorpusPaths, meeting_id: str) -> tuple[list[Any], str]:
    """The meeting's turns with REAL times, and its corpus ("ami"/"icsi").

    Raises:
        HarnessError: the alignment rate is under the injection tooling's floor, which would
            otherwise silently fall back to synthetic times. A gate input never does.
    """
    from app.scripts.corpus_injection.adapters.qmsum import QMSumAdapter
    from app.scripts.corpus_injection.timings import resolve_timings

    adapter = QMSumAdapter(paths.qmsum, ami_root=paths.ami, icsi_root=paths.icsi)
    doc = adapter.load(meeting_id)
    resolve_timings(doc)
    if not doc.timing.is_real:
        raise HarnessError(
            f'{meeting_id}: word alignment rate {doc.timing.alignment_rate:.2f} is under the '
            'floor; refusing synthetic timings for a gate input',
            ExitCode.MISUSE,
        )
    return doc.turns, ('ami' if doc.extra['domain'] == 'Product' else 'icsi')


def render_input(spec: InputSpec, paths: CorpusPaths) -> RenderedInput:
    """Render ``spec`` exactly as the app summarizes a transcript (plan section 3.3)."""
    from app.scripts.corpus_injection.nxt import strip_markers
    from app.utils.transcript_builders import build_transcript_and_stats

    segments: list[_Segment] = []
    labels: dict[str, str] = {}
    offsets: dict[str, float] = {}
    cursor = 0.0
    for meeting_id in spec.meetings:
        turns, corpus = load_timed_turns(paths, meeting_id)
        identities = _ami_identities(paths, meeting_id) if corpus == 'ami' else {}
        offset = 0.0 if not segments else cursor + CONCAT_GAP_S
        offsets[meeting_id] = offset
        for turn in sorted(turns, key=lambda t: (t.start, t.end)):
            text = strip_markers(turn.text)
            if not text:
                continue
            who = identities.get(turn.speaker, turn.speaker)
            # SPEAKER_NN, in order of first appearance: what the app prints for speakers nobody
            # has named, and the only form the chunker's turn regex matches. Named labels would
            # silently push every slice boundary onto the sentence splitter.
            label = labels.setdefault(who, f'SPEAKER_{len(labels):02d}')
            segments.append(_Segment(text, turn.start + offset, turn.end + offset, _Speaker(label)))
            cursor = max(cursor, turn.end + offset)
    transcript, stats = build_transcript_and_stats(segments, None, voiceprint_names=True)
    return RenderedInput(transcript, stats, offsets)


# ---------------------------------------------------------------------------- chunking


def make_chunker(max_tokens: int) -> Any:
    """A real `LLMService` (never contacted) whose `max_tokens` fixes the slice layout."""
    ensure_app_environment()
    from app.core.enums import LLMProvider
    from app.services.llm_service import LLMService
    from app.services.llm_types import LLMConfig

    config = LLMConfig(
        provider=LLMProvider.VLLM,
        model='plan-slices',
        base_url='http://127.0.0.1:9/v1',
        max_tokens=max_tokens,
    )
    return LLMService(config)


def chunk_transcript(transcript: str, max_tokens: int) -> tuple[list[str], int]:
    """The app's own slicing at ``max_tokens``; returns the chunks and the response budget."""
    service = make_chunker(max_tokens)
    return service._chunk_transcript_intelligently(transcript), service.response_tokens


def chunk_start_seconds(chunk: str) -> int | None:
    """Seconds of the first `[MM:SS]` stamp in a chunk (stamps run past 99 minutes)."""
    match = _STAMP.search(chunk)
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def slice_index_for(time_s: float, chunks: list[str]) -> int:
    """1-based slice holding the instant ``time_s``: chunk i spans [start_i, start_{i+1})."""
    starts = [chunk_start_seconds(c) for c in chunks]
    index = 1
    for i, start in enumerate(starts, start=1):
        if start is not None and time_s >= start:
            index = i
    return index


def located_per_slice(gold_times: list[float | None], chunks: list[str]) -> dict[int, int]:
    """How many located gold items fall in each slice (unlocated items are not counted here)."""
    counts: dict[int, int] = dict.fromkeys(range(1, len(chunks) + 1), 0)
    for time_s in gold_times:
        if time_s is not None:
            counts[slice_index_for(time_s, chunks)] += 1
    return counts


# ---------------------------------------------------------------------------- planning


@dataclass
class SlicePlan:
    """The locked layout of one input."""

    max_tokens: int
    response_tokens: int
    chunk_chars: list[int]
    chunk_sha256: list[str]
    tail_index: int | None
    near_misses: list[dict[str, int]] = field(default_factory=list)


def _tail_candidates(
    render: RenderedInput, gold_times: list[float | None]
) -> tuple[list[tuple[int, int]], list[dict[str, int]]]:
    """``[(distance to 386 chars, max_tokens)]`` that qualify, plus the nearest misses."""
    qualifying: list[tuple[int, int]] = []
    misses: list[dict[str, int]] = []
    seen: set[tuple[str, ...]] = set()
    for max_tokens in TAIL_SEARCH:
        chunks, _ = chunk_transcript(render.transcript, max_tokens)
        key = tuple(sha256(c) for c in chunks)
        if key in seen:
            continue
        seen.add(key)
        if len(chunks) < 3:  # at least 2 substantive slices plus the tail
            continue
        size = len(chunks[-1])
        holds_gold = bool(located_per_slice(gold_times, chunks)[len(chunks)])
        if TAIL_MIN_CHARS <= size <= TAIL_MAX_CHARS and not holds_gold:
            qualifying.append((abs(size - TAIL_TARGET_CHARS), max_tokens))
        else:
            misses.append(
                {'max_tokens': max_tokens, 'tail_chars': size, 'gold_in_tail': int(holds_gold)}
            )
    misses.sort(key=lambda m: abs(m['tail_chars'] - TAIL_TARGET_CHARS))
    return sorted(qualifying), misses[:5]


def plan_slices(
    spec: InputSpec, render: RenderedInput, gold_times: list[float | None]
) -> SlicePlan:
    """Pick the ``max_tokens`` that makes the app's chunker produce the layout ``spec`` needs.

    Raises:
        HarnessError: no value qualifies (exit 3); the nearest misses are in the message.
    """
    if spec.kind == KIND_TAIL:
        qualifying, misses = _tail_candidates(render, gold_times)
        if not qualifying:
            raise HarnessError(
                f'{spec.input_id}: no max_tokens in {TAIL_SEARCH.start}-{TAIL_SEARCH.stop - 1} '
                f'leaves a {TAIL_MIN_CHARS}-{TAIL_MAX_CHARS} char tail free of gold items; '
                f'nearest misses: {misses}',
                ExitCode.PRECONDITION,
            )
        max_tokens = qualifying[0][1]
        chunks, response = chunk_transcript(render.transcript, max_tokens)
        return _plan(max_tokens, response, chunks, tail_index=len(chunks), misses=misses)
    for max_tokens in range(LONG_FLOOR_TOKENS, CONTEXT_CEILING + 1, 50):
        chunks, response = chunk_transcript(render.transcript, max_tokens)
        if len(chunks) in LONG_CHUNKS:
            return _plan(max_tokens, response, chunks, tail_index=None, misses=[])
    raise HarnessError(
        f'{spec.input_id}: no max_tokens in {LONG_FLOOR_TOKENS}-{CONTEXT_CEILING} yields '
        f'{LONG_CHUNKS} slices',
        ExitCode.PRECONDITION,
    )


def _plan(
    max_tokens: int,
    response: int,
    chunks: list[str],
    *,
    tail_index: int | None,
    misses: list[dict[str, int]],
) -> SlicePlan:
    return SlicePlan(
        max_tokens=max_tokens,
        response_tokens=response,
        chunk_chars=[len(c) for c in chunks],
        chunk_sha256=[sha256(c) for c in chunks],
        tail_index=tail_index,
        near_misses=misses,
    )


def failing_slice(located: dict[int, int], chunk_count: int, tail_index: int | None) -> int:
    """The substantive slice with the most located gold items; ties go to the earlier slice.

    Never the last slice, which would overlap criterion (c).
    """
    eligible = [i for i in range(1, chunk_count) if i != tail_index]
    return max(eligible, key=lambda i: (located.get(i, 0), -i))


def verify_against_lock(entry: dict[str, Any], transcript: str) -> list[str]:
    """Re-chunk with the locked ``max_tokens``; returns the mismatches (empty means identical)."""
    chunks, response = chunk_transcript(transcript, entry['max_tokens'])
    problems: list[str] = []
    if sha256(transcript) != entry['transcript_sha256']:
        problems.append('transcript hash differs')
    if response != entry['response_tokens']:
        problems.append(f'response_tokens {response} != locked {entry["response_tokens"]}')
    if len(chunks) != len(entry['chunks']):
        problems.append(f'{len(chunks)} chunks != locked {len(entry["chunks"])}')
    else:
        problems += [
            f'chunk {i} hash differs'
            for i, (c, locked) in enumerate(zip(chunks, entry['chunks'], strict=True), start=1)
            if sha256(c) != locked['sha256']
        ]
    return problems
