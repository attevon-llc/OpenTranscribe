"""AMI distractor adapter — the 34 AMI meetings QMSum never selected (issue #461 A5).

QMSum's ``Product`` domain redistributes 137 of AMI's 171 meetings (see
:mod:`app.scripts.corpus_injection.adapters.qmsum`); this adapter injects the other
**34** (measured against the real corpus, not assumed: EN 16, IN 10, IB 7, TS 1) as a
same-domain, differently-worded HAYSTACK for QMSum's own gold queries to be measured
against. It manufactures **no judgements of its own** — every query QMSum's gold set
still addresses only QMSum's 137 meetings; these 34 exist purely to make retrieval work
harder, the way a real deployment's index is never just the files a benchmark cares about.

⚠️ **Injecting this changes what every retrieval number MEASURES.** A run against
QMSum-only and a run against QMSum+AMI-distractors are not comparable, and nothing in
this module enforces that — see ``scripts/benchmark_rag.py``'s ``injection_identity``
(runinfo, never metrics.json) and ``rag-evaluation.md``'s "AMI distractor haystack"
section for the guard that makes a stale comparison refuse instead of silently drifting.

Unlike ``qmsum.py``'s Product/Academic timing recovery, these meetings need no diff-based
alignment (:func:`~..nxt.align_turns_to_channels`) against a second, independently
redistributed transcript. AMI's own ``segments.xml``/``words.xml`` files ARE the source:
each ``<segment>`` already carries curator-set ``transcriber_start``/``transcriber_end``
boundaries and a ``nite:child`` reference into that channel's timed words. Reusing the
diff aligner here would be reaching for the tool built to reconcile QMSum's *redistributed
text* against a reference it is not derived from — there is no second transcript to
reconcile, so every turn gets real times on every channel, always (100% alignment, not an
``align_turns_to_channels`` measured rate).

What this module DOES reuse: :func:`~..nxt.read_meeting_channels` — the same
``corpusResources/meetings.xml`` parse :attr:`~..adapters.qmsum.QMSumAdapter._ami_channels`
is built on — for participant metadata, and the same XML-trust rationale (see ``nxt.py``'s
module docstring: these files are the same pinned, SHA-256-verified NAS archive fetched by
``scripts/fetch-rag-eval-data.sh``, not user input).

⚠️ **`meetings.xml` under-lists a real meeting's channels** — ``IN1001`` lists 3 `<speaker>`
entries (A/B/C) but ships 4 channels' worth of ``segments.xml``/``words.xml`` (A-D), measured
directly against the real corpus. Channel *presence* therefore comes from the
``segments``/``words`` directory listing, never from ``meetings.xml`` — that file is used
only to attach a friendlier speaker label where one exists (AMI's four-role Scenario/Product
protocol, ``PM``/``ID``/``UI``/``ME``), falling back to ``"Participant <channel>"`` for the
33 of 34 distractor meetings that carry no ``role`` attribute at all.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: S405  # nosec B405 — see nxt.py's XML trust note
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from app.scripts.corpus_injection.adapters.base import CorpusAdapter
from app.scripts.corpus_injection.adapters.qmsum import AMI_ROLE_TO_QMSUM
from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn
from app.scripts.corpus_injection.model import Word
from app.scripts.corpus_injection.nxt import read_meeting_channels

# NXT's namespace prefix. ElementTree resolves `nite:id`/`nite:child` to this full URI as
# the attribute/tag key once the file declares `xmlns:nite=...` — see nxt.py's module note.
_NITE_NS = "{http://nite.sourceforge.net/}"
_NITE_ID_ATTR = f"{_NITE_NS}id"
_NITE_CHILD_TAG = f"{_NITE_NS}child"

# "<file>.words.xml#id(<meeting>.<channel>.words<N>)" or "...#id(<start>)..id(<end>)" for a
# multi-word span. Both forms appear in the real corpus (~90% are ranges; measured on
# EN2001a.A: 123 of 136 segments).
_HREF_RANGE_RE = re.compile(r"#id\(([^)]+)\)(?:\.\.id\(([^)]+)\))?")
_ID_SUFFIX_RE = re.compile(r"(\d+)$")

_AMI_DIR_RE = re.compile(r"^ami_public_manual_([0-9.]+)$")


@dataclass(slots=True)
class _TimedElement:
    """One NXT word-tier element, keyed by its numeric ``nite:id`` suffix (not position)."""

    tag: str
    text: str
    start: float | None
    end: float | None


def _read_words_by_id(path: Path) -> dict[int, _TimedElement]:
    """Parse one ``*.words.xml``, keyed by its numeric ``nite:id`` suffix.

    ``segments.xml`` addresses a turn's boundary by ``nite:id`` (``...words0``,
    ``...words13``, ...), and that id space is shared across ``<w>``, ``<vocalsound>``,
    ``<disfmarker>`` and ``<gap>`` siblings — an utterance can start or end on any of them
    (checked against the real corpus). :func:`~..nxt.read_channel_words` cannot be reused
    here: it returns a POSITIONAL list with punctuation dropped and clitics merged, which is
    exactly right for diffing against an independent token stream and exactly wrong for
    resolving an ``nite:id`` range — the position of an id in that filtered list no longer
    matches its numeric suffix once anything upstream was skipped or merged.
    """
    root = ET.parse(path).getroot()  # noqa: S314  # nosec B314 — see module note on XML trust
    out: dict[int, _TimedElement] = {}
    for el in root:
        raw_id = el.get(_NITE_ID_ATTR)
        if not raw_id:
            continue
        match = _ID_SUFFIX_RE.search(raw_id)
        if not match:
            continue
        start_raw, end_raw = el.get("starttime"), el.get("endtime")
        out[int(match.group(1))] = _TimedElement(
            tag=el.tag.rsplit("}", 1)[-1],
            text=(el.text or "").strip(),
            start=float(start_raw) if start_raw else None,
            end=float(end_raw) if end_raw else None,
        )
    return out


#: Leading characters that attach directly to the previous token with no space: closing
#: punctuation (``"Okay ."`` -> ``"Okay."``) and clitics NXT splits off (``"I" "'ve"`` ->
#: ``"I've"``, the same fold-back ``nxt.read_channel_words`` does for the alignment path).
_NO_SPACE_BEFORE = frozenset(".,!?;:)'’")


def _join_words(elements: list[_TimedElement]) -> str:
    """Join a segment's word-tier elements into display text.

    ``<vocalsound>``/``<disfmarker>``/``<gap>`` carry no text at all and are dropped rather
    than rendered as empty tokens.
    """
    parts: list[str] = []
    for el in elements:
        if el.tag != "w" or not el.text:
            continue
        if parts and el.text[0] not in _NO_SPACE_BEFORE:
            parts.append(" ")
        parts.append(el.text)
    return "".join(parts).strip()


def _segment_id_range(href: str) -> tuple[int, int] | None:
    match = _HREF_RANGE_RE.search(href)
    if not match:
        return None
    start_raw, end_raw = match.group(1), match.group(2) or match.group(1)
    start_match, end_match = _ID_SUFFIX_RE.search(start_raw), _ID_SUFFIX_RE.search(end_raw)
    if not start_match or not end_match:
        return None
    return int(start_match.group(1)), int(end_match.group(1))


def _read_channel_turns(segments_path: Path, words_path: Path, speaker: str) -> list[Turn]:
    """One channel's ``segments.xml`` + ``words.xml`` -> timed, unindexed turns.

    ``turn_index`` is left at 0 for every turn here — the caller assigns the real,
    chronological index once every channel's turns are merged, because AMI's index has no
    meaning per channel (unlike QMSum's, which is load-bearing for gold-span addressing this
    adapter does not use at all: these meetings carry no queries).

    A segment with no ``<w>`` element at all (pure ``<vocalsound>`` — a laugh, a cough) is
    dropped rather than emitted as an empty-text turn. Measured across all 34 distractors:
    23,049 of 25,269 raw ``<segment>`` elements (91.2%) carry real text; the rest are
    non-verbal-only and correctly contribute nothing to the index.
    """
    words_by_id = _read_words_by_id(words_path)
    root = ET.parse(segments_path).getroot()  # noqa: S314  # nosec B314 — see module note
    turns: list[Turn] = []
    for segment in root:
        if segment.tag.rsplit("}", 1)[-1] != "segment":
            continue
        start_raw = segment.get("transcriber_start")
        end_raw = segment.get("transcriber_end")
        child = segment.find(_NITE_CHILD_TAG)
        if start_raw is None or end_raw is None or child is None:
            continue
        href = child.get("href") or ""
        id_range = _segment_id_range(href)
        if id_range is None:
            continue
        lo, hi = id_range
        elements = [words_by_id[i] for i in range(lo, hi + 1) if i in words_by_id]
        text = _join_words(elements)
        if not text:
            continue
        words = [
            Word(el.text, el.start, el.end)
            for el in elements
            if el.tag == "w" and el.start is not None and el.end is not None
        ]
        turns.append(
            Turn(
                turn_index=0,
                speaker=speaker,
                text=text,
                start=float(start_raw),
                end=float(end_raw),
                words=words or None,
            )
        )
    return turns


class AMIDistractorAdapter(CorpusAdapter):
    """Inject AMI meetings QMSum's ``Product`` domain does not already cover.

    Args:
        root: ``$RAG_EVAL_DATA_DIR/ami`` (or its extracted subdirectory).
        qmsum_root: ``$RAG_EVAL_DATA_DIR/qmsum`` — read-only, to compute the exclusion set.
            Required, not optional: without it there is no way to know which meetings QMSum
            already redistributed, and injecting one of those a second time under a different
            key would double a real meeting's content in the index rather than add a distinct
            distractor to it.
    """

    key = "ami"

    def __init__(self, root: Path, qmsum_root: Path) -> None:
        super().__init__(root)
        self.qmsum_root = Path(qmsum_root)

    # ---------------------------------------------------------------- layout

    @cached_property
    def _extracted(self) -> Path:
        """The directory directly holding ``corpusResources/``, ``segments/``, ``words/``."""
        if (self.root / "corpusResources").is_dir():
            return self.root
        candidates = sorted(
            p for p in self.root.iterdir() if p.is_dir() and (p / "corpusResources").is_dir()
        )
        if not candidates:
            raise FileNotFoundError(
                f"No extracted AMI tree with corpusResources/ under {self.root}"
            )
        return candidates[-1]

    @cached_property
    def _version(self) -> str:
        match = _AMI_DIR_RE.match(self._extracted.name)
        return match.group(1) if match else self._extracted.name

    @cached_property
    def _meetings_xml(self) -> Path:
        return self._extracted / "corpusResources" / "meetings.xml"

    @cached_property
    def _channel_labels(self) -> dict[str, dict[str, dict[str, str]]]:
        return read_meeting_channels(self._meetings_xml)

    @cached_property
    def _qmsum_product_ids(self) -> frozenset[str]:
        """Meeting ids QMSum's ``Product`` domain already redistributes.

        Reads the raw QMSum tree directly (glob for ``data/Product/all/*.json``) rather than
        going through :class:`~.qmsum.QMSumAdapter` — that class's ``meeting_ids()`` spans all
        three domains and this only needs the AMI-sourced one, without the AMI/ICSI timing
        roots QMSumAdapter otherwise requires.
        """
        candidates = [self.qmsum_root, *sorted(p for p in self.qmsum_root.iterdir() if p.is_dir())]
        for candidate in candidates:
            product_dir = candidate / "data" / "Product" / "all"
            if product_dir.is_dir():
                return frozenset(p.stem for p in product_dir.glob("*.json"))
        raise FileNotFoundError(f"No QMSum data/Product/all/ tree found under {self.qmsum_root}")

    def describe(self) -> CorpusInfo:
        return CorpusInfo(
            key=self.key,
            name="AMI Meeting Corpus (distractor haystack)",
            version=self._version,
            license_tier="A",
            root=str(self._extracted),
            citation="Carletta et al., The AMI Meeting Corpus, MLMI 2005. CC BY 4.0. "
            "Injected as a same-domain retrieval distractor, not a source of judgements — "
            "QMSum's own gold queries (see qmsum.py) are the only relevance judgements over "
            "this content.",
        )

    def meeting_ids(self) -> list[str]:
        root = ET.parse(self._meetings_xml).getroot()  # noqa: S314  # nosec B314 — see nxt.py
        all_ids = {m.get("observation") for m in root if m.get("observation")}
        distractors = all_ids - self._qmsum_product_ids
        return sorted(m for m in distractors if m)

    # ------------------------------------------------------------- loading

    def _channels(self, meeting_id: str) -> list[str]:
        """Channel letters that actually have segments+words files on disk.

        Never trusts ``meetings.xml``'s `<speaker>` list for this — see the module
        docstring's warning about ``IN1001``.
        """
        segments_dir = self._extracted / "segments"
        pattern = re.compile(rf"^{re.escape(meeting_id)}\.([A-Za-z0-9]+)\.segments\.xml$")
        found = []
        for path in segments_dir.glob(f"{meeting_id}.*.segments.xml"):
            match = pattern.match(path.name)
            if match:
                found.append(match.group(1))
        return sorted(found)

    def _speaker_label(self, meeting_id: str, channel: str) -> str:
        info = self._channel_labels.get(meeting_id, {}).get(channel)
        role = (info or {}).get("role", "")
        return AMI_ROLE_TO_QMSUM.get(role, f"Participant {channel}")

    def load(self, meeting_id: str) -> MeetingDoc:
        segments_dir = self._extracted / "segments"
        words_dir = self._extracted / "words"
        merged: list[Turn] = []
        for channel in self._channels(meeting_id):
            speaker = self._speaker_label(meeting_id, channel)
            merged.extend(
                _read_channel_turns(
                    segments_dir / f"{meeting_id}.{channel}.segments.xml",
                    words_dir / f"{meeting_id}.{channel}.words.xml",
                    speaker,
                )
            )
        merged.sort(key=lambda t: (t.start if t.start is not None else 0.0, t.speaker))
        for index, turn in enumerate(merged):
            turn.turn_index = index

        aligned = sum(1 for t in merged if t.start is not None and t.end is not None)
        doc = MeetingDoc(
            corpus=self.key,
            meeting_id=meeting_id,
            title=f"AMI (distractor) — {meeting_id}",
            turns=merged,
            language="en",
            timing=TimingInfo(
                source=TIMING_REAL,
                reference=f"ami:{meeting_id}",
                aligned_turns=aligned,
                total_turns=len(merged),
            ),
            extra={
                "role": "distractor",
                "license_tier": "A",
                "channels": len(self._channels(meeting_id)),
            },
        )
        return doc
