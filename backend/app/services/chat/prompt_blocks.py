"""Evidence rendering for RAG chat prompts: excerpts and the counted, recurrence
and synthesis blocks.

Every function here concatenates only (never ``str.format``/``Template``) and runs
text through :mod:`app.services.chat.prompt_sanitize` first; see ``prompting.py``
for the injection-resistance rationale and the layered system prompt.
"""

from __future__ import annotations

from app.services.chat.prompt_sanitize import _sanitize_attribute
from app.services.chat.prompt_sanitize import _sanitize_body_text
from app.services.chat.prompt_sanitize import _sanitize_chunk_text
from app.services.chat.redactor import MaskedChunk

_EXCERPT_HEADER = "Transcript excerpts:\n\n"
_EXCERPT_CLOSE = "\n</excerpt>\n\n"
# Appended to an excerpt that was cut to fit the budget, so the trailing text
# never reads as a completed sentence.
_TRUNCATION_MARK = " […]"
# Below this there is nothing worth grounding an answer in, so a first excerpt
# that cannot be trimmed to at least this much is skipped rather than shown as a
# fragment.
#
# This number is INDEPENDENT of citations.py's SNIPPET_CHARS, and must stay
# that way (issue #913 made them diverge — 240 here vs. a wider, indexing-
# target-derived cap there). The two answer different questions: this one
# decides whether a truncated PROMPT excerpt is worth sending to the model at
# all, citations.py's decides how much of an ALREADY-SENT excerpt the citation
# CARD shows the reader afterward. "Keep them in sync" is the wrong instinct
# it invites — that edit would change what reaches the prompt, not just what
# the UI displays, and 240 stands on its own here regardless of what the card
# shows.
_MIN_TRUNCATED_EXCERPT_CHARS = 240


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def _excerpt_open_tag(index: int, chunk: MaskedChunk, *, truncated: bool = False) -> str:
    """Build one excerpt's opening tag.

    Attribute VALUES are sanitized here: they are user-controlled strings (file
    titles, speaker names), not trusted metadata. The transcript body is only
    ever concatenated, never interpolated.
    """
    speaker = _sanitize_attribute(chunk.speaker or "") or "Unknown speaker"
    title = _sanitize_attribute(chunk.title or "") or "Untitled recording"
    flag = ' truncated="true"' if truncated else ""
    return (
        f'<excerpt id="{index}" recording="{title}" '
        f'speaker="{speaker}" time="{_clock(chunk.start_time)}"{flag}>\n'
    )


def _cut_at_boundary(text: str, limit: int) -> str:
    """Trim ``text`` to at most ``limit`` chars, preferring a sentence boundary.

    A mid-word cut invites the model to complete the word itself, so fall back
    through sentence → word → hard cut. The ``limit // 2`` floor keeps the search
    from throwing away most of the excerpt to honour a boundary near the start.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence_end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("\n"))
    if sentence_end >= limit // 2:
        return cut[: sentence_end + 1].rstrip()
    word_end = cut.rfind(" ")
    if word_end >= limit // 2:
        return cut[:word_end].rstrip()
    return cut.rstrip()


def format_excerpts(chunks: list[MaskedChunk], *, budget_chars: int) -> tuple[str, list[int]]:
    """Render chunks as delimited excerpts, stopping at the character budget.

    Chunks are consumed in the order given (already rerank-ordered), so the
    budget truncates the least relevant material.

    **The budget is a hard ceiling** (issue #387). It used to be advisory for the
    first excerpt — the loop could not break on iteration one — so a single long
    speaker turn overran the room reserved for the answer and the failure landed
    provider-side (a 400, or silent truncation) rather than here. A first excerpt
    that does not fit is now trimmed to what does, tagged ``truncated="true"``,
    and skipped entirely when even the trimmed form would be too short to ground
    an answer.

    Args:
        chunks: Masked chunks, most relevant first.
        budget_chars: Ceiling on the rendered block. Never exceeded.

    Returns:
        ``(rendered_block, excerpt_ids)`` — the 1-based ids actually emitted, in
        order. Callers MUST build citations from these ids rather than from the
        input list, or the UI offers sources the model never saw (issue #384).
    """
    if not chunks:
        return "", []

    parts: list[str] = [_EXCERPT_HEADER]
    used_ids: list[int] = []
    total = len(_EXCERPT_HEADER)

    for index, chunk in enumerate(chunks, start=1):
        content = _sanitize_chunk_text(chunk.content).strip()
        if not content:
            continue

        block = _excerpt_open_tag(index, chunk) + content + _EXCERPT_CLOSE
        if total + len(block) > budget_chars:
            if used_ids:
                # Later chunks are less relevant; stop rather than cherry-pick a
                # shorter one out of rank order.
                break
            trimmed = _trim_to_budget(index, chunk, content, budget_chars - total)
            if trimmed is None:
                # No room for anything worth reading from this chunk. A shorter
                # one further down may still fit whole.
                continue
            block = trimmed

        parts.append(block)
        total += len(block)
        used_ids.append(index)

    if not used_ids:
        return "", []
    return "".join(parts), used_ids


def _trim_to_budget(index: int, chunk: MaskedChunk, content: str, room: int) -> str | None:
    """Render one excerpt cut down to ``room`` chars, or None if it cannot be.

    Only ever applied to the FIRST excerpt: dropping it outright would answer
    from no transcript context at all, while emitting it whole would overrun the
    budget that reserves space for the reply.
    """
    open_tag = _excerpt_open_tag(index, chunk, truncated=True)
    content_room = room - len(open_tag) - len(_EXCERPT_CLOSE) - len(_TRUNCATION_MARK)
    if content_room < _MIN_TRUNCATED_EXCERPT_CHARS:
        return None
    return open_tag + _cut_at_boundary(content, content_room) + _TRUNCATION_MARK + _EXCERPT_CLOSE


# A counted answer is small, exact, and IS the answer — so it is rendered before
# the excerpts and its cost comes off the top of the budget. Rows are capped so a
# 400-file result cannot crowd out every excerpt; the cap is stated in the block
# rather than silently applied, because a truncated list read as complete is the
# same silent-wrong-answer shape the whole tier exists to remove.
_MAX_COUNTED_ROWS = 40
_COUNTED_OPEN = "<counted>\n"
_COUNTED_CLOSE = "</counted>\n\n"


def _format_seconds(total: float) -> str:
    """``742.3`` -> ``"12m 22s"``. Plain, so the model quotes a readable figure."""
    total_int = int(round(total))
    minutes, seconds = divmod(max(total_int, 0), 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_counted_block(result) -> str:
    """Render an :class:`~app.services.chat.aggregation.AggregationResult`.

    Concatenation only, and every value is sanitized the same way an excerpt
    attribute is — a recording title is arbitrary user text and reaches the
    prompt here just as it does inside an ``<excerpt>`` tag.

    Args:
        result: The aggregation result, or ``None``.

    Returns:
        A delimited block, or ``""`` when there is nothing counted to report.
    """
    if result is None:
        return ""
    lines: list[str] = [f"question type: {_sanitize_attribute(result.shape)}"]
    if result.subject:
        lines.append(f"counted for: {_sanitize_attribute(result.subject)}")
    if result.count is not None:
        lines.append(f"total: {int(result.count)}")
    if result.speaker:
        # `speaker_seconds` (SHAPE_SPEAKER_STATS, exact talk time) and
        # `speaker_sessions` (SHAPE_SPEAKER_FACET: attendance) are different
        # units of the same "top speaker" idea and never both set — rendering
        # sessions for a talk-time result would report "(0 recordings)" next
        # to a name that may have appeared in every recording in scope.
        if result.speaker_seconds is not None:
            lines.append(
                f"top speaker: {_sanitize_attribute(result.speaker)} "
                f"({_format_seconds(result.speaker_seconds)} talk time)"
            )
        else:
            lines.append(
                f"top speaker: {_sanitize_attribute(result.speaker)} "
                f"({int(result.speaker_sessions or 0)} recordings)"
            )

    titles = list(result.file_titles) or [""] * len(result.file_uuids)
    if result.file_uuids:
        lines.append("recordings:")
        for title in titles[:_MAX_COUNTED_ROWS]:
            shown = _sanitize_attribute(title) or "Untitled recording"
            lines.append(f"  - {shown}")
        hidden = len(result.file_uuids) - min(len(result.file_uuids), _MAX_COUNTED_ROWS)
        if hidden > 0:
            lines.append(f"  (+{hidden} more not listed here; the total above is complete)")
    for name, value in sorted(result.coverage.items()):
        if value is not None:
            lines.append(f"note: {_sanitize_attribute(f'{name} = {value}')}")
    return _COUNTED_OPEN + "\n".join(lines) + "\n" + _COUNTED_CLOSE


# Same row-cap posture as `_MAX_COUNTED_ROWS` — a 1,500-item recurrence
# scan can produce more groups than a prompt can afford to list, and a
# truncated list read as complete is the same silent-wrong-answer shape the
# rest of this module exists to remove.
_MAX_RECURRENCE_ROWS = 30
_RECURRENCE_OPEN = "<recurrence>\n"
_RECURRENCE_CLOSE = "</recurrence>\n\n"


def format_recurrence_block(result) -> str:
    """Render a :class:`~app.services.chat.recurrence.RecurrenceResult`.

    Same sanitization posture as :func:`format_counted_block`: every value
    that originated as user/model text (a representative item, an owner
    name) goes through :func:`_sanitize_attribute`, since this is a short
    discrete value interpolated into a delimited block, not free-running
    prose. Deliberately does NOT import
    ``app.services.chat.recurrence`` — ``result`` is duck-typed, matching
    :func:`format_counted_block`'s treatment of ``AggregationResult``, so
    this module never needs to import that dataclass just to type a
    parameter.

    Args:
        result: A ``RecurrenceResult``, or ``None``.

    Returns:
        A delimited block, or ``""`` when there is nothing to report — either
        ``result`` is ``None`` or it ran and found zero recurring groups AND
        has no honesty note worth surfacing (an empty scan with nothing
        declined or truncated says nothing a model needs told).
    """
    if result is None:
        return ""
    groups = list(getattr(result, "groups", ()) or ())
    coverage = dict(getattr(result, "coverage", {}) or {})
    truncated = bool(getattr(result, "truncated", False))
    declined_for_language = int(getattr(result, "declined_for_language", 0) or 0)
    declined_languages = list(getattr(result, "declined_languages", ()) or ())
    masking_failed = int(coverage.get("masking_failed_files") or 0)

    if not groups and not truncated and not declined_for_language and not masking_failed:
        return ""

    lines: list[str] = [f"recurring items: {len(groups)}"]
    lines.append(
        "note: this data does not track whether an item is open or completed — "
        "report only that it recurred"
    )
    for group in groups[:_MAX_RECURRENCE_ROWS]:
        text = _sanitize_attribute(getattr(group, "representative_text", "")) or "(untitled item)"
        files = len(getattr(group, "file_uuids", ()) or ())
        owners = [_sanitize_attribute(o) for o in (getattr(group, "owners", ()) or ())]
        owners = [o for o in owners if o]
        suffix = f" (owners: {', '.join(owners)})" if owners else ""
        lines.append(f'- "{text}" — {files} recordings{suffix}')
    hidden = len(groups) - min(len(groups), _MAX_RECURRENCE_ROWS)
    if hidden > 0:
        lines.append(
            f"(+{hidden} more recurring items not listed here; the count above is complete)"
        )
    if truncated:
        lines.append(
            f"note: scanned {getattr(result, 'considered', 0)} items and stopped there — "
            "more may exist in scope but were not examined"
        )
    if declined_for_language:
        langs = ", ".join(declined_languages) or "unknown"
        lines.append(
            f"note: {declined_for_language} item(s) in {langs} were excluded — recurrence "
            "detection does not yet support that script"
        )
    if masking_failed:
        lines.append(
            f"note: {masking_failed} recording(s) were excluded because their content "
            "could not be safely masked"
        )
    return _RECURRENCE_OPEN + "\n".join(lines) + "\n" + _RECURRENCE_CLOSE


# #403. Same posture as `format_recurrence_block`: a bounded, sanitized
# block that precedes the excerpts. Unlike `counted`/`overview`/`recurrence`,
# its text is FREE-RUNNING MODEL PROSE (the enrichment call's own reply), not
# assembled from discrete named fields — so it goes through the body-safe
# sanitizer (`_sanitize_body_text`, unbounded length, tag-defusing only)
# rather than the attribute one, exactly like an excerpt's transcript
# content is sanitized.
_SYNTHESIS_OPEN = "<synthesis>\n"
_SYNTHESIS_CLOSE = "\n</synthesis>\n\n"


def format_synthesis_block(text: str) -> str:
    """Wrap an enrichment reply as a delimited ``<synthesis>`` block.

    Args:
        text: The enrichment call's own reply — machine-drafted, and base
            rule 15 (added alongside this) tells the model to treat it as a
            draft to verify against the excerpts, not a second source of
            truth.

    Returns:
        A delimited block, or ``""`` when there is nothing to wrap.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    return _SYNTHESIS_OPEN + _sanitize_body_text(clean) + _SYNTHESIS_CLOSE
