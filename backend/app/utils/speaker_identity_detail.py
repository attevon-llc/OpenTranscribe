"""The small, structured origin records stored beside a speaker's identity and suggestion (#1280).

``speaker.identity_source_detail`` says where the CURRENT identity came from and
``speaker.suggestion_detail`` where the CURRENT recommendation came from. Both are JSONB
objects built ONLY by the functions below so their shape stays closed and small:

* ids (uuids, provider and model identifiers, task names), never a person's name and never
  transcript text;
* ``None`` values are dropped, so a key is present exactly when the writer knew it;
* ``origin`` is always present and always one of the closed sets below.

Pure functions: nothing here touches the database. The writers that store the result are the
``record_*`` helpers in :mod:`app.utils.speaker_identity_source`.

This is the *current* state only. The full append-only history of who changed what and when is
tracked in issue #1336 (v0.7.0).
"""

from __future__ import annotations

from numbers import Integral
from numbers import Real
from typing import Any
from typing import SupportsFloat

# --- identity origins -------------------------------------------------------------------------

#: A voiceprint compared the speaker against a stored voice and wrote the identity.
ORIGIN_VOICE_MATCH = "voice_match"
#: A voiceprint compared the speaker against a profile's consolidated voice.
ORIGIN_PROFILE_MATCH = "profile_match"
#: A voiceprint path linked the speaker to a profile that a person had already named it as.
ORIGIN_PROFILE_PROPAGATION = "profile_propagation"
#: Upload-time clustering or a re-cluster placed the speaker in a promoted cluster.
ORIGIN_CLUSTER_JOIN = "cluster_join"
#: A skip marker: a person dismissed a voice suggestion, which the privacy rules treat as the
#: voiceprint's.
ORIGIN_VOICE_SUGGESTION_SKIPPED = "voice_suggestion_skipped"
#: The v449 upgrade recognised the shape of a voiceprint auto-accept in existing data.
ORIGIN_V449_BACKFILL = "v449_backfill"

MACHINE_ORIGINS: frozenset[str] = frozenset(
    {
        ORIGIN_VOICE_MATCH,
        ORIGIN_PROFILE_MATCH,
        ORIGIN_PROFILE_PROPAGATION,
        ORIGIN_CLUSTER_JOIN,
        ORIGIN_VOICE_SUGGESTION_SKIPPED,
        ORIGIN_V449_BACKFILL,
    }
)

#: A person typed the name.
ORIGIN_TYPED = "typed"
#: A person accepted a pending suggestion (a voice match or an LLM guess).
ORIGIN_ACCEPTED_SUGGESTION = "accepted_suggestion"
#: A person assigned an existing profile to the speaker.
ORIGIN_PROFILE_ASSIGNED = "profile_assigned"
#: A person created a new profile for the speaker.
ORIGIN_PROFILE_CREATED = "profile_created"
#: A person assigned a profile or accepted a name for several speakers at once.
ORIGIN_BATCH_ASSIGN = "batch_assign"
#: A person named a cluster (its members take the name).
ORIGIN_CLUSTER_PROMOTED = "cluster_promoted"
#: A person merged two promoted clusters (the moved speakers take the target's profile).
ORIGIN_CLUSTER_MERGED = "cluster_merged"
#: A person created the speaker with a name.
ORIGIN_CREATED = "created"
#: A person confirmed the speaker's existing name or link (``verified: true``).
ORIGIN_CONFIRMED = "confirmed"

HUMAN_ORIGINS: frozenset[str] = frozenset(
    {
        ORIGIN_TYPED,
        ORIGIN_ACCEPTED_SUGGESTION,
        ORIGIN_PROFILE_ASSIGNED,
        ORIGIN_PROFILE_CREATED,
        ORIGIN_BATCH_ASSIGN,
        ORIGIN_CLUSTER_PROMOTED,
        ORIGIN_CLUSTER_MERGED,
        ORIGIN_CREATED,
        ORIGIN_CONFIRMED,
    }
)

# --- suggestion origins -----------------------------------------------------------------------

#: A language model guessed the name. Never applied automatically.
ORIGIN_LLM = "llm"
#: A person dismissed the speaker from the Inbox without choosing a name.
ORIGIN_USER_SKIPPED = "user_skipped"

SUGGESTION_ORIGINS: frozenset[str] = frozenset(
    {ORIGIN_VOICE_MATCH, ORIGIN_PROFILE_MATCH, ORIGIN_LLM, ORIGIN_USER_SKIPPED}
)

#: ``suggestion_source`` values the snapshot of an accepted suggestion may copy from.
_NOT_A_SUGGESTION_SOURCES = frozenset({"user_skipped"})


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values and round floats, so a stored record is small and stable."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            out[key] = value
        elif isinstance(value, Integral):
            out[key] = int(value)
        else:
            # Includes numpy scalars, which json cannot encode as they are.
            out[key] = round(float(value), 4)
    return out


def _require(origin: str, allowed: frozenset[str], kind: str) -> str:
    if origin not in allowed:
        raise ValueError(f"unknown {kind} origin {origin!r}")
    return origin


def voiceprint_detail(
    origin: str,
    *,
    confidence: SupportsFloat | None = None,
    threshold: SupportsFloat | None = None,
    matched_speaker_uuid: Any = None,
    matched_profile_uuid: Any = None,
    embedding_model: str | None = None,
    origin_task: str | None = None,
) -> dict[str, Any]:
    """Origin of an identity or a suggestion a voiceprint produced.

    Args:
        origin: One of :data:`MACHINE_ORIGINS` (``voice_match`` / ``profile_match`` for a
            suggestion too).
        confidence: The raw cosine similarity of the match.
        threshold: The confidence the matcher required to act on it.
        matched_speaker_uuid: The stored speaker the voice matched (a voice match).
        matched_profile_uuid: The profile whose consolidated voice matched (a profile match).
        embedding_model: The identifier of the embedding model that produced the voice.
        origin_task: The task or service that wrote it, when no person asked for it.
    """
    return _compact(
        {
            "origin": _require(origin, MACHINE_ORIGINS | SUGGESTION_ORIGINS, "voiceprint"),
            "confidence": confidence,
            "threshold": threshold,
            "matched_speaker_uuid": str(matched_speaker_uuid) if matched_speaker_uuid else None,
            "matched_profile_uuid": str(matched_profile_uuid) if matched_profile_uuid else None,
            "embedding_model": embedding_model,
            "origin_task": origin_task,
        }
    )


def llm_detail(
    *,
    provider: str | None,
    model: str | None,
    confidence: SupportsFloat | None = None,
    prompt_version: str | None = None,
    origin_task: str | None = None,
) -> dict[str, Any]:
    """Origin of a suggestion a language model made."""
    return _compact(
        {
            "origin": ORIGIN_LLM,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "confidence": confidence,
            "origin_task": origin_task,
        }
    )


def accepted_suggestion_snapshot(
    speaker: Any, *, require_name_match: bool = True
) -> dict[str, Any] | None:
    """What a person accepted: the speaker's pending suggestion, summarised without ids.

    ``None`` when the speaker carries no suggestion, or, with ``require_name_match``, when the
    name now on the speaker is a different one (a person who typed another name did not accept
    this suggestion; an explicit "accept" action passes ``False``). Matched
    speaker and profile ids are left out on purpose: a person's own record must stay valid
    while voiceprints are off, and those ids describe a voice comparison.
    """
    name = getattr(speaker, "suggested_name", None)
    source = getattr(speaker, "suggestion_source", None)
    if not name or not source or source in _NOT_A_SUGGESTION_SOURCES:
        return None
    current = getattr(speaker, "display_name", None)
    if (
        require_name_match
        and isinstance(current, str)
        and current.strip()
        and current.strip().casefold() != str(name).strip().casefold()
    ):
        return None
    snapshot: dict[str, Any] = {
        "source": str(source),
        "confidence": getattr(speaker, "confidence", None),
    }
    detail = getattr(speaker, "suggestion_detail", None)
    if isinstance(detail, dict) and detail.get("origin") == ORIGIN_LLM:
        snapshot["llm"] = _compact(
            {"provider": detail.get("provider"), "model": detail.get("model")}
        )
    return _compact(snapshot)


def human_detail(
    origin: str,
    *,
    accepted_suggestion: dict[str, Any] | None = None,
    origin_task: str | None = None,
) -> dict[str, Any]:
    """Origin of an identity a person made or confirmed."""
    return _compact(
        {
            "origin": _require(origin, HUMAN_ORIGINS, "human"),
            "accepted_suggestion": accepted_suggestion,
            "origin_task": origin_task,
        }
    )
