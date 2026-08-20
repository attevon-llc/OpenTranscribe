"""Canonical speaker display-label resolution — the SINGLE home for it.

Three planes resolved a speaker's display name differently, and the disagreement was a
live correctness bug, not a cosmetic one: ``transcript_builders.get_speaker_name`` emitted
``"Alice (suggested)"``, ``SpeakerStatusService`` ignored suggestions and an unverified
``display_name`` entirely, and the chunk index wrote bare ``"Unknown"`` while
``file_facts`` wrote ``"Unknown Speaker"``. A digest built from one plane and a facts
roster built from another could name the same speaker two different ways in the same
answer.

This module is the one place that decides. ``facts.py``, ``digest.py``,
``transcript_builders.get_speaker_name`` and ``speaker_status_service`` all call
:func:`canonical_speaker_label`; the chunk-index writers (``tasks/search_indexing_task.py``,
``tasks/reindex_task.py``) and a handful of API-response formatters
(``api/endpoints/files/crud.py::_resolve_segment_speaker_name``,
``services/formatting_service.py``) do **not** yet — they are outside this change's file
set and are the remaining work to close the disagreement completely.
"""

from __future__ import annotations

#: The canonical "not diarized to a person" label. Chosen over the bare "Unknown" some
#: planes used because it is unambiguous next to a real name in a roster or a facet —
#: "Unknown" alone reads as "an unnamed *thing*", not "nobody was attributed here".
UNKNOWN_SPEAKER_LABEL = "Unknown Speaker"

#: Every spelling that has meant "not diarized to a person" somewhere in this codebase.
#: A caller filtering an aggregate (a roster, a facet, a "who's in this" list) must
#: exclude the whole set, not just :data:`UNKNOWN_SPEAKER_LABEL` — the chunk-index writers
#: and the API formatters above still emit the bare ``"Unknown"``, so checking only the
#: canonical spelling would silently let those rows back in as if they were a real person
#: named "Unknown".
UNKNOWN_SPEAKER_LABELS: frozenset[str] = frozenset({UNKNOWN_SPEAKER_LABEL, "Unknown"})

#: The confidence a suggestion needs before it is shown at all. Matches the value both
#: prior implementations used (``get_speaker_name``'s ``0.75`` literal and
#: ``SpeakerStatusService.HIGH_CONFIDENCE_THRESHOLD``) — not a new number, just named once.
DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD = 0.75


def canonical_speaker_label(
    name: str | None,
    *,
    display_name: str | None = None,
    suggested_name: str | None = None,
    confidence: float | None = None,
    suggestion_threshold: float = DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD,
) -> str:
    """Resolve the one canonical display label for a speaker.

    Priority: **any** set ``display_name`` (a human labeled this speaker, verified or not
    — more authoritative than an automatic guess) > a confident ``suggested_name`` > the
    raw diarization ``name`` > :data:`UNKNOWN_SPEAKER_LABEL`.

    This deliberately merges two behaviours that used to disagree.
    ``transcript_builders.get_speaker_name`` required ``verified`` before trusting
    ``display_name`` at all, falling back to the raw ``name`` for an unverified one — so a
    user-entered-but-not-yet-clicked-verify name was silently ignored in every summary and
    digest. ``SpeakerStatusService._resolve_display_name`` used any ``display_name``
    unconditionally but never looked at a suggestion. Preferring any ``display_name`` over
    a suggestion is the union: a human's label (however provisional) outranks a machine's
    guess, and *verified* only matters for status/audit purposes, which callers with
    the ``verified`` flag can still report separately (it is not part of the label).

    **Never returns a ``"(suggested)"`` decoration.** That was an English literal baked
    into the label text itself — a later multilingual pass would have had to detect and
    strip it, and it duplicated ``computed_status``/``status_text``, which already
    communicate "this is an unverified suggestion" without touching the name. A caller
    that needs to know a returned label came from a suggestion checks ``confidence`` /
    whether ``display_name`` was set, not the string.

    Args:
        name: The raw diarization label (``Speaker.name``), or ``None`` when there is no
            linked speaker at all.
        display_name: A user-set display name, verified or not.
        suggested_name: An LLM/embedding speaker-ID suggestion. Per the root CLAUDE.md,
            suggestions are never auto-applied to the record — this is a *display*
            fallback only, exactly as ``get_speaker_name`` used before this helper existed.
        confidence: The suggestion's confidence, 0-1.
        suggestion_threshold: Minimum confidence for the suggestion to be shown.

    Returns:
        A non-empty label. :data:`UNKNOWN_SPEAKER_LABEL` when nothing resolves.
    """
    if display_name:
        return str(display_name)
    if suggested_name and confidence is not None and confidence >= suggestion_threshold:
        return str(suggested_name)
    if name:
        return str(name)
    return UNKNOWN_SPEAKER_LABEL
