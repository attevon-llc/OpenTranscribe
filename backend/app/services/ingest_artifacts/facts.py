"""``file_facts.facts`` — the deterministic, exactly-answerable half of the summary tier.

Nothing in here is inferred. Duration, roster, talk-time split, turn count and longest
monologue all fall out of the ``transcript_segment`` rows, so Stage 4 can answer "who
talked most", "how long was it", "who was in this" and "which of these meetings ran over
an hour" *exactly*, with no LLM in the loop and no retrieval step that could miss a file.
That is the aggregation tier's floor, and #403 **D6** is why it has to exist without a
provider configured.

Per-speaker talk time, segment count and word count come from
``utils.transcript_builders.compute_speaker_stats``, which is the function
``build_transcript_and_stats`` has always used and then discarded. The turn-level
statistics (turn count, longest monologue, first/last utterance) are new, and need the
segments in **total order** — ``(start_time, end_time, id)`` — because a turn is defined
by adjacency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: Bumped when the payload's shape changes.
FACTS_SCHEMA_VERSION = 1


def _turn_stats(segments: list[dict[str, Any]]) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Walk ordered segments once, returning (turn_count, per_speaker, longest_monologue)."""
    per_speaker: dict[str, dict[str, Any]] = {}
    longest: dict[str, Any] = {
        "speaker": None,
        "seconds": 0.0,
        "start_time": None,
        "end_time": None,
    }
    turn_count = 0

    current_speaker: str | None = None
    turn_start = 0.0
    turn_end = 0.0

    def close_turn() -> None:
        nonlocal longest
        if current_speaker is None:
            return
        duration = max(turn_end - turn_start, 0.0)
        entry = per_speaker.setdefault(current_speaker, {"turn_count": 0, "longest_turn": 0.0})
        entry["turn_count"] += 1
        entry["longest_turn"] = max(entry["longest_turn"], duration)
        # Strict >: on a tie the earlier monologue wins, so the answer does not depend on
        # which of two equal-length turns the walk happened to reach last.
        if duration > longest["seconds"]:
            longest = {
                "speaker": current_speaker,
                "seconds": round(duration, 2),
                "start_time": round(turn_start, 2),
                "end_time": round(turn_end, 2),
            }

    for segment in segments:
        speaker = str(segment.get("speaker") or "Unknown Speaker")
        start = float(segment.get("start_time") or 0.0)
        end = float(segment.get("end_time") or 0.0)
        if speaker != current_speaker:
            close_turn()
            turn_count += 1
            current_speaker = speaker
            turn_start = start
            turn_end = end
        else:
            turn_end = max(turn_end, end)
    close_turn()

    return turn_count, per_speaker, longest


def build_facts(
    segments: list[dict[str, Any]],
    *,
    speaker_stats: dict[str, Any],
    duration: float | None,
    language: str | None,
    recorded_at: datetime | None,
) -> dict[str, Any]:
    """Assemble the ``facts`` payload.

    Args:
        segments: Segment dicts in total order, keys ``id``/``text``/``start_time``/
            ``end_time``/``speaker``. ``speaker`` is the resolved display name, so the
            roster reads as the user's own labels — and so a rename is a regeneration
            trigger (addendum G1, issue #405).
        speaker_stats: Output of ``utils.transcript_builders.compute_speaker_stats``.
        duration: ``MediaFile.duration`` in seconds, when known.
        language: Detected language code.
        recorded_at: ``MediaFile.upload_time`` — the only date the pipeline actually has.
            Named ``recorded_at`` in the payload because Stage 4's temporal router asks
            "when was this", not "when was the row written".

    Returns:
        The ``file_facts.facts`` JSONB payload.
    """
    turn_count, turn_by_speaker, longest = _turn_stats(segments)

    word_count = sum(len(str(s.get("text") or "").split()) for s in segments)
    spoken_seconds = sum(float(stats.get("total_time") or 0.0) for stats in speaker_stats.values())

    speakers: list[dict[str, Any]] = [
        {
            "name": name,
            "total_time": round(float(stats.get("total_time") or 0.0), 2),
            "segment_count": int(stats.get("segment_count") or 0),
            "word_count": int(stats.get("word_count") or 0),
            "percentage": round(float(stats.get("percentage") or 0.0), 2),
            "turn_count": int(turn_by_speaker.get(name, {}).get("turn_count", 0)),
            "longest_turn": round(float(turn_by_speaker.get(name, {}).get("longest_turn", 0.0)), 2),
        }
        for name, stats in sorted(speaker_stats.items())
    ]
    # Presentation order: loudest first, name as the tiebreak. Sorting a dict's keys and
    # then re-sorting by a float is two total orders composed, so the list is stable even
    # when two speakers hold the floor for exactly the same time.
    speakers.sort(key=lambda s: (-s["total_time"], s["name"]))

    return {
        "schema_version": FACTS_SCHEMA_VERSION,
        "duration_seconds": round(float(duration), 2) if duration else None,
        "recorded_at": recorded_at.isoformat() if recorded_at else None,
        "language": language,
        "segment_count": len(segments),
        "turn_count": turn_count,
        "word_count": word_count,
        "spoken_seconds": round(spoken_seconds, 2),
        "speaker_count": len(speakers),
        "roster": sorted(speaker_stats),
        "speakers": speakers,
        "longest_monologue": longest,
        "first_utterance_at": round(float(segments[0].get("start_time") or 0.0), 2)
        if segments
        else None,
        "last_utterance_at": round(float(segments[-1].get("end_time") or 0.0), 2)
        if segments
        else None,
    }
