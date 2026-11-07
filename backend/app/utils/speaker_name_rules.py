"""What a speaker name or a suggestion source IS, with no dependencies (leaf module).

Split out of :mod:`app.utils.speaker_labels` so that code the models layer imports (the identity
history recorder, issue #1336) can ask these questions without reaching ``speaker_labels``'s
function-level import of the privacy view. ``speaker_labels`` imports them from here.
"""

from __future__ import annotations

import re

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
