"""Canonical speaker display-label resolution — the SINGLE home for it.

Three planes resolved a speaker's display name differently, and the disagreement was a
live correctness bug, not a cosmetic one: ``transcript_builders.get_speaker_name`` emitted
``"Alice (suggested)"``, ``SpeakerStatusService`` ignored suggestions and an unverified
``display_name`` entirely, and the chunk index wrote bare ``"Unknown"`` while
``file_facts`` wrote ``"Unknown Speaker"``. A digest built from one plane and a facts
roster built from another could name the same speaker two different ways in the same
answer.

This module is the one place that decides, and every plane now goes through it (issue #620
item 8c — this docstring previously listed four of them as "not yet"; verified current,
Aug 2026). ``facts.py``, ``digest.py``, ``transcript_builders.get_speaker_name`` and
``speaker_status_service`` call :func:`canonical_speaker_label` directly; the chunk-index
writers (``tasks/search_indexing_task.py::resolve_chunk_speaker_name``,
``tasks/reindex_task.py::_resolve_reindex_speaker_name``) and the API-response formatters
(``api/endpoints/files/crud.py::_resolve_segment_speaker_name``,
``services/formatting_service.py``) delegate to it through their own thin resolution
helpers — see ``tests/unit/test_canonical_speaker_label.py``'s cross-plane agreement tests,
which call each writer's real helper directly (never a second call to
``canonical_speaker_label`` re-proving the function against itself) so a writer that
reverted to its own ad hoc chain would fail the comparison.
"""

from __future__ import annotations

import re
from typing import Any

#: The canonical "not diarized to a person" label. Chosen over the bare "Unknown" some
#: planes used because it is unambiguous next to a real name in a roster or a facet —
#: "Unknown" alone reads as "an unnamed *thing*", not "nobody was attributed here".
UNKNOWN_SPEAKER_LABEL = "Unknown Speaker"

#: Every spelling that has meant "not diarized to a person" somewhere in this codebase.
#: A caller filtering an aggregate (a roster, a facet, a "who's in this" list) must
#: exclude the whole set, not just :data:`UNKNOWN_SPEAKER_LABEL` — every writer now emits
#: only the canonical spelling going forward, but OpenSearch documents indexed BEFORE this
#: module unified them still carry the bare ``"Unknown"`` literal, and nothing reindexes
#: them proactively. Checking only the canonical spelling would silently let those legacy
#: rows back in as if they were a real person named "Unknown". Keep this entry until a
#: full reindex is known to have retired every pre-unification document.
UNKNOWN_SPEAKER_LABELS: frozenset[str] = frozenset({UNKNOWN_SPEAKER_LABEL, "Unknown"})

#: A ``display_name`` matching this is a **diarization placeholder**, not a human-assigned
#: identity — as are ``NULL`` and ``""``. The distinction is deliberate and shared with the
#: frontend's speaker plane: a placeholder is scoped to **one file**, so ``SPEAKER_00`` in
#: recording A and ``SPEAKER_00`` in recording B are different people and must never be
#: treated as one cross-file identity. Anything else is a name a human assigned.
PLACEHOLDER_SPEAKER_NAME_PATTERN = r"^SPEAKER_\d+$"


def is_assigned_speaker_name(value: object) -> bool:
    """Whether a ``display_name`` names somebody, rather than being empty or a placeholder."""
    if not isinstance(value, str) or not value.strip():
        return False
    return re.match(PLACEHOLDER_SPEAKER_NAME_PATTERN, value.strip()) is None


#: The confidence a suggestion needs before it is shown at all. Matches the value both
#: prior implementations used (``get_speaker_name``'s ``0.75`` literal and
#: ``SpeakerStatusService.HIGH_CONFIDENCE_THRESHOLD``) — not a new number, just named once.
DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD = 0.75

#: ``Speaker.suggestion_source`` values written ONLY by a voiceprint/embedding match
#: (``speaker_matching_service`` for ``profile_match`` / ``voice_match``,
#: ``speaker_update`` and ``batch_speaker_matching`` for ``voice_match``). Their
#: ``confidence`` is a cosine similarity gated at ``CLUSTER_ASSIGNMENT_THRESHOLD`` (0.75), so
#: a suggestion carrying one of them at >= 0.75 is evidence about the VOICE.
#:
#: Owner decision 2026-10-12: a speaker's canonical label (what search, chat, exports and the
#: OpenSearch documents show as the name) comes only from a name a human typed/verified or
#: from one of these. Every other source -- ``llm_analysis``, ``metadata_hint``,
#: ``user_skipped``, and a row with NO source (legacy data that cannot prove where its
#: suggestion came from) -- is a suggestion the human must verify: shown in suggestion UIs
#: with its confidence, never promoted to the speaker's name. This is an allow-list on
#: purpose: a new suggestion producer is non-canonical until someone decides otherwise.
EMBEDDING_SUGGESTION_SOURCES: frozenset[str] = frozenset({"voice_match", "profile_match"})


def is_embedding_suggestion_source(source: object) -> bool:
    """Whether a ``suggestion_source`` marks a voiceprint match (see the allow-list above)."""
    return isinstance(source, str) and source in EMBEDDING_SUGGESTION_SOURCES


#: Words that mean the model is NOT naming anyone: "Unknown (not Robert)", "N/A", "Unidentified".
_NON_NAME_PATTERN = re.compile(
    r"\b(unknown|unidentified|unnamed|unclear|unsure|uncertain|undetermined|anonymous|"
    r"not|none|null|n/?a|cannot|can't|unable|no name)\b|^speaker[\s_-]*\d*$",
    re.IGNORECASE,
)
_MAX_PERSON_NAME_CHARS = 80


def looks_like_a_person_name(value: object) -> bool:
    """Whether an LLM ``predicted_name`` is a name rather than a statement about not having one.

    The model answers "who is this speaker?" with prose when it cannot tell ("Unknown (not
    Robert)"). Stored as ``suggested_name`` that string is shown to the user as the
    suggestion, so it is refused at the door.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > _MAX_PERSON_NAME_CHARS:
        return False
    return _NON_NAME_PATTERN.search(text) is None


def canonical_speaker_label(
    name: str | None,
    *,
    display_name: str | None = None,
    suggested_name: str | None = None,
    confidence: float | None = None,
    suggestion_source: str | None = None,
    suggestion_threshold: float = DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD,
    verified: bool | None = None,
    identity_source: str | None = None,
    voiceprint_names: bool = True,
) -> str:
    """Resolve the one canonical display label for a speaker.

    Priority: **any** set ``display_name`` (a human labeled this speaker, verified or not
    — more authoritative than an automatic guess) > a ``suggested_name`` that a
    **voiceprint match** produced at ``confidence >= 0.75`` (``suggestion_source`` in
    :data:`EMBEDDING_SUGGESTION_SOURCES`) > the raw diarization ``name`` >
    :data:`UNKNOWN_SPEAKER_LABEL`. An LLM suggestion (``llm_analysis``) -- or any suggestion
    whose source is not a voiceprint match, or unknown -- never reaches the label, however
    confident: the human verifies it first, which sets ``display_name``.

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
        suggested_name: A speaker-ID suggestion (LLM or voiceprint). Only a voiceprint one
            can become the label; see ``suggestion_source``.
        confidence: The suggestion's confidence, 0-1 (a cosine similarity for a voiceprint
            suggestion).
        suggestion_source: ``Speaker.suggestion_source``. Omitted/``None`` means the
            provenance is unknown, which fails closed: the suggestion is ignored.
        suggestion_threshold: Minimum confidence for the suggestion to be shown.
        verified: ``Speaker.verified``. Only consulted when ``voiceprint_names`` is False: a
            voiceprint suggestion a person verified is theirs and stays.
        identity_source: ``Speaker.identity_source`` (see ``utils/speaker_identity_source``).
            Only consulted when ``voiceprint_names`` is False.
        voiceprint_names: False when the speaker's file has voiceprints off (#1280): a
            ``display_name`` a voiceprint wrote (``identity_source = 'voiceprint'``) is
            skipped, and a voiceprint suggestion is not promoted unless a person verified it
            on a row whose identity is not machine-made. Everything else resolves as when
            True; the caller decides the policy, this function only applies it.

    Returns:
        A non-empty label. :data:`UNKNOWN_SPEAKER_LABEL` when nothing resolves.
    """
    from app.utils.speaker_identity_source import VOICEPRINT
    from app.utils.speaker_identity_source import hides_name

    if display_name and not hides_name(identity_source, voiceprint_names):
        return str(display_name)
    suggestion_withheld = not voiceprint_names and (
        verified is not True or identity_source == VOICEPRINT
    )
    if (
        not suggestion_withheld
        and suggested_name
        and is_embedding_suggestion_source(suggestion_source)
        and confidence is not None
        and confidence >= suggestion_threshold
        # Rows stored before predictions were validated can hold "Unknown (not Robert)".
        and looks_like_a_person_name(suggested_name)
    ):
        return str(suggested_name)
    if name:
        return str(name)
    return UNKNOWN_SPEAKER_LABEL


def canonical_speaker_label_for_row(
    row: Any, db: Any = None, *, voiceprint_names: bool | None = None
) -> str:
    """The label a (re)index would write for this ``Speaker`` row, right now.

    Issue #605: eight repair/propagation call sites (``rename_propagation_task``,
    ``speakers.py``, ``speaker_update.py``, ``speaker_matching_service.py``,
    ``speaker_clustering_service.py``) computed the "old"/"current" indexed name
    with the ad hoc ``display_name or name`` chain, which stopped agreeing with
    the chunk-index writer the moment the writer moved to
    :func:`canonical_speaker_label` (this module's own docstring flagged the
    writers as migrated and the propagation plane as NOT). A propagation task
    computing the wrong "old name" narrows an ``update_by_query`` to a filter
    that matches nothing — it logs ``status: success`` and the drift survives.

    This wrapper exists so every such call site imports ONE function instead of
    re-unpacking the same attributes with its own copy of the rule — a copy
    is exactly how the eight sites drifted in the first place.

    **Honours the privacy policy** (#1280, plan D5): while the row's file has voiceprints
    off, a name a voiceprint produced is left out, whether it is an unverified voiceprint
    suggestion or a ``display_name`` a voiceprint path wrote (``identity_source =
    'voiceprint'``). A label printed by an export, a caption, the search index or the chat
    roster then agrees with the speaker responses. The policy is asked only when the
    answer could change the label (the common row costs no query).

    ``voiceprint_names`` lets a caller that already holds the file's decision pass it:
    ``True`` = policy-free (what the data says; the stored status columns use this), ``False``
    = withhold, ``None`` (default) = ask the policy through ``db``. ``db`` is a session or a
    ``PrivacyView`` for a columns-only row (which has no session of its own and must then
    also expose ``media_file_id``, ``verified`` and ``identity_source``); an ORM row uses its
    own session.

    **A columns-only row must select** ``Speaker.identity_source``. When it does not, the row
    names somebody, and the file's voiceprints are off, this raises ``TypeError`` rather than
    guess: printing the name could leak a voiceprint's work, and hiding it would hide a
    person's. A forgotten column therefore fails the first test that turns the switch off.

    Args:
        row: Anything exposing ``.name`` / ``.display_name`` / ``.suggested_name``
            / ``.confidence`` / ``.suggestion_source`` — a ``Speaker`` ORM instance, a SQLAlchemy
            columns-only row/tuple with matching attribute names, or a test
            stand-in.
        db: A session or ``PrivacyView`` for a columns-only row; see above.
        voiceprint_names: The file's voiceprint decision when the caller already has it.

    Returns:
        The canonical display label — never empty, never the bare ``"Unknown"``.

    Raises:
        TypeError: The row lacks ``identity_source``, names somebody, and its file's
            voiceprints are off (or voiceprint_names is False).
    """
    # getattr: a stand-in or columns-only row that predates the provenance rule
    # degrades to "unknown source" (suggestion ignored) rather than raising.
    lacks_identity = not hasattr(row, "identity_source")
    identity_source = None if lacks_identity else row.identity_source

    def label(voiceprints: bool) -> str:
        return canonical_speaker_label(
            row.name,
            display_name=row.display_name,
            suggested_name=getattr(row, "suggested_name", None),
            confidence=getattr(row, "confidence", None),
            suggestion_source=getattr(row, "suggestion_source", None),
            verified=getattr(row, "verified", None),
            identity_source=identity_source,
            voiceprint_names=voiceprints,
        )

    shown = label(True)
    if voiceprint_names is True:
        return shown
    withheld = label(False)
    unknown_name = lacks_identity and is_assigned_speaker_name(row.display_name)
    if shown == withheld and not unknown_name:
        return shown  # nothing the policy could change: no query
    if voiceprint_names is None:
        from app.services.privacy.redact_view import voiceprints_off_for_row

        if not voiceprints_off_for_row(row, db):
            return shown
    if unknown_name:
        raise TypeError("row lacks identity_source; select Speaker.identity_source")
    return withheld
