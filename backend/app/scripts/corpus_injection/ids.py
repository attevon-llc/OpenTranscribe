"""Deterministic identifiers for injected eval corpora (issue #403).

Every identifier an injected meeting carries is a pure function of
``(corpus, seed, meeting_id)``. Two consequences the harness depends on:

* **Idempotency** — a re-run computes the same ``file_uuid``, finds the existing
  row, and updates or skips it instead of creating a second copy.
* **Traceability** — a result recorded against a ``file_uuid`` months later can
  be resolved back to the exact source meeting without consulting a database.

The seed exists so an isolated namespace (a test run, a second index arm) can be
injected alongside the canonical one without colliding. An empty seed is the
canonical namespace.
"""

from __future__ import annotations

import hashlib
import uuid

# Fixed namespace for OpenTranscribe RAG-eval corpus injection. Generated once
# and frozen: changing it renumbers every corpus ever injected.
CORPUS_NAMESPACE = uuid.UUID("4b0f2f8e-1d3a-5c76-9a41-6f2c8d7e0b53")

_SEP = "\x1f"


def _key(corpus: str, meeting_id: str, seed: str, kind: str) -> str:
    return _SEP.join((kind, corpus, seed, meeting_id))


def file_uuid(corpus: str, meeting_id: str, seed: str = "") -> uuid.UUID:
    """UUID for the ``MediaFile`` standing in for one source meeting."""
    return uuid.uuid5(CORPUS_NAMESPACE, _key(corpus, meeting_id, seed, "file"))


def segment_uuid(corpus: str, meeting_id: str, seed: str, index: int) -> uuid.UUID:
    """UUID for the Nth ``TranscriptSegment`` of a meeting.

    Indexed by position in the emitted segment list, so a re-run that produces
    the same segments reuses the same uuids rather than churning them.
    """
    return uuid.uuid5(CORPUS_NAMESPACE, f"{_key(corpus, meeting_id, seed, 'segment')}{_SEP}{index}")


def speaker_uuid(corpus: str, meeting_id: str, seed: str, speaker_name: str) -> uuid.UUID:
    """UUID for a per-file ``Speaker`` row."""
    return uuid.uuid5(
        CORPUS_NAMESPACE, f"{_key(corpus, meeting_id, seed, 'speaker')}{_SEP}{speaker_name}"
    )


def content_sha256(turns) -> str:  # noqa: ANN001 — Iterable[Turn], typed loosely to avoid a cycle
    """Fingerprint the exact transcript content injected for a meeting.

    Covers speaker label and text for every turn in order. Timings are
    deliberately excluded: the same transcript aligned against a timed reference
    and the same transcript with synthetic times are the *same content*, and
    conflating them would make the fingerprint useless for answering "did the
    corpus text change?".
    """
    h = hashlib.sha256()
    for turn in turns:
        h.update(turn.speaker.encode("utf-8"))
        h.update(b"\x1f")
        h.update(turn.text.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()
