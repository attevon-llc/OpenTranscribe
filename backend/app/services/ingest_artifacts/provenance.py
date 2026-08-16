"""Digest provenance — a tagged union, from day one (#403 D3).

Every digest sentence records *where in the source it came from*. Transcripts locate
text by ``TranscriptSegment`` id; documents (#362 / Stage 6) locate it by character
offset into the parsed IR. Those are genuinely different addressing schemes, and the
decision recorded in #403 D3 is that the discriminated shape exists **now**, while the
only producer is the transcript path — retrofitting it once document digests need to
join the summary tier would mean a second migration over stored JSONB.

Two variants::

    {"kind": "segment_ids", "segment_ids": [12, 13], "start_time": 30.1, "end_time": 41.0}
    {"kind": "char_range",  "char_start": 1024, "char_end": 1310, "page": 4}

``kind`` is the discriminator and is always present. Readers switch on it; they must
never infer the variant from which optional keys happen to be set, because that is
exactly the check that stops working when a third variant lands.

Why the transcript variant carries timestamps as well as ids: addendum **G7**. A digest
document indexed with ``start_time=0`` deep-links a citation to ``0:00``, which is a
plausible-looking wrong answer. The extractive design already knows the originating
segments, so it can pay for real timestamps here instead of making Stage 4 re-derive
them with a DB round trip per citation.
"""

from __future__ import annotations

from typing import Any
from typing import Literal
from typing import TypedDict

#: Discriminator value for transcript-derived text.
KIND_SEGMENT_IDS = "segment_ids"

#: Discriminator value for document-derived text (produced by #362 / Stage 6).
KIND_CHAR_RANGE = "char_range"

#: Every value ``kind`` may take. A reader that does not recognise a kind must treat the
#: provenance as unusable rather than guessing — see :func:`validate_provenance`.
PROVENANCE_KINDS: tuple[str, ...] = (KIND_SEGMENT_IDS, KIND_CHAR_RANGE)


class SegmentProvenance(TypedDict):
    """Transcript provenance: which ``TranscriptSegment`` rows produced this sentence."""

    kind: Literal["segment_ids"]
    segment_ids: list[int]
    start_time: float
    end_time: float


class CharRangeProvenance(TypedDict, total=False):
    """Document provenance: a half-open character range into the parsed IR text."""

    kind: Literal["char_range"]
    char_start: int
    char_end: int
    page: int | None


Provenance = SegmentProvenance | CharRangeProvenance


class ProvenanceError(ValueError):
    """A provenance payload is missing its discriminator or its variant's fields."""


def segment_provenance(
    segment_ids: list[int], start_time: float, end_time: float
) -> SegmentProvenance:
    """Build transcript provenance.

    Args:
        segment_ids: Ids of the ``TranscriptSegment`` rows the sentence spans. Stored
            **sorted**: the caller may collect them in any order, and an unsorted list
            makes two runs over identical data produce different JSONB.
        start_time: Onset of the first contributing segment, seconds.
        end_time: End of the last contributing segment, seconds.

    Returns:
        A ``segment_ids``-kinded provenance dict.
    """
    return {
        # The literal, not KIND_SEGMENT_IDS: a TypedDict's Literal field cannot be
        # satisfied by a module-level `str` constant. The two are pinned equal by
        # tests/unit/test_ingest_artifacts_provenance.py.
        "kind": "segment_ids",
        "segment_ids": sorted(set(segment_ids)),
        "start_time": round(float(start_time), 2),
        "end_time": round(float(end_time), 2),
    }


def char_range_provenance(
    char_start: int, char_end: int, page: int | None = None
) -> CharRangeProvenance:
    """Build document provenance (the #362 / Stage 6 variant).

    Unused by the transcript pipeline; it exists so the union has both arms and both
    arms are tested. Stage 6 calls this rather than inventing a parallel shape.

    Args:
        char_start: Inclusive start offset into the document's IR text.
        char_end: Exclusive end offset.
        page: 1-based page number when the parser knows one.

    Returns:
        A ``char_range``-kinded provenance dict.
    """
    if char_end < char_start:
        raise ProvenanceError(f"char_end {char_end} precedes char_start {char_start}")
    payload: CharRangeProvenance = {
        "kind": "char_range",  # see the note in segment_provenance
        "char_start": int(char_start),
        "char_end": int(char_end),
    }
    if page is not None:
        payload["page"] = int(page)
    return payload


def validate_provenance(payload: Any) -> None:
    """Raise :class:`ProvenanceError` unless *payload* is a well-formed variant.

    Used by the artifact builder before persisting and by Stage 3 before indexing, so a
    malformed provenance fails at the producer instead of surfacing as a citation that
    points nowhere.
    """
    if not isinstance(payload, dict):
        raise ProvenanceError(f"provenance must be a dict, got {type(payload).__name__}")
    kind = payload.get("kind")
    if kind not in PROVENANCE_KINDS:
        raise ProvenanceError(
            f"unknown provenance kind {kind!r}; expected one of {PROVENANCE_KINDS}"
        )
    if kind == KIND_SEGMENT_IDS:
        ids = payload.get("segment_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
            raise ProvenanceError("segment_ids provenance needs a non-empty list of int ids")
        if ids != sorted(ids):
            raise ProvenanceError("segment_ids must be stored sorted (determinism)")
        for key in ("start_time", "end_time"):
            if not isinstance(payload.get(key), (int, float)):
                raise ProvenanceError(f"segment_ids provenance needs a numeric {key}")
    else:
        for key in ("char_start", "char_end"):
            if not isinstance(payload.get(key), int):
                raise ProvenanceError(f"char_range provenance needs an int {key}")
        if payload["char_end"] < payload["char_start"]:
            raise ProvenanceError("char_range end precedes start")


def provenance_timespan(payload: Provenance) -> tuple[float, float] | None:
    """Return ``(start_time, end_time)`` for time-addressable provenance, else ``None``.

    Stage 4's citation builder calls this instead of reading ``start_time`` directly:
    a document digest has no timestamp, and ``None`` is the signal to emit a file-level
    citation rather than a deep link to ``0:00`` (addendum G7).
    """
    if payload.get("kind") != KIND_SEGMENT_IDS:
        return None
    return float(payload["start_time"]), float(payload["end_time"])  # type: ignore[typeddict-item]
