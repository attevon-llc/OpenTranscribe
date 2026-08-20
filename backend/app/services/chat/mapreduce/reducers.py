"""The REDUCE half: file summaries become one collection view.

Split out of the former single-file ``mapreduce.py``. Two reducers share one
interface (:meth:`.reduce`); :class:`CodeComposer` (no LLM, first class per
**D6**) and :class:`BatchReducer` (many small bounded LLM calls, never one
impossible one). See the package docstring in ``__init__.py`` for the
map-reduce framing this whole subpackage implements.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.chat.mapreduce.file_summaries import FileSummary
from app.services.chat.mapreduce.overview import _OVERVIEW_CLOSE
from app.services.chat.mapreduce.overview import _OVERVIEW_OPEN
from app.services.chat.mapreduce.overview import DEFAULT_BATCH_FILES
from app.services.chat.mapreduce.overview import MAX_LISTED_FILES
from app.services.chat.mapreduce.overview import MAX_REDUCE_CALLS
from app.services.chat.mapreduce.overview import Overview
from app.services.chat.mapreduce.overview import _clock

logger = logging.getLogger(__name__)


def _corpus_header(summaries: list[FileSummary], files_in_scope: int = 0) -> list[str]:
    """The facts that are true of the whole scope, and are exact.

    The roster and keyphrase list are sanitized with
    ``prompting._sanitize_body_text`` — NOT ``_sanitize_attribute`` — because
    speaker display names are OWNER-controlled on a shared recording: the
    person who named "Dana" is not necessarily the person chatting, so an
    unescaped ``</overview><synthesis>`` in a display name would be cross-user
    prompt injection into the highest-trust part of the prompt the model is
    told to treat as authoritative. See the parent CLAUDE.md's "Assembly is
    concatenation-only" section for why this is the body-safe sanitizer and
    not the attribute one.
    """
    from app.services.chat.prompting import _sanitize_body_text

    dates = sorted(s.recorded_at for s in summaries if s.recorded_at)
    total_seconds = sum(float(s.duration or 0.0) for s in summaries)
    roster: dict[str, None] = {}
    for summary in summaries:
        for name in summary.speakers:
            roster.setdefault(_sanitize_body_text(name), None)

    counts: dict[str, int] = {}
    for summary in summaries:
        for phrase in summary.keyphrases:
            safe_phrase = _sanitize_body_text(phrase)
            counts[safe_phrase] = counts.get(safe_phrase, 0) + 1
    # Recurring means recurring: a phrase from one recording is that recording's
    # topic, not the collection's.
    recurring = sorted(
        (phrase for phrase, n in counts.items() if n > 1),
        key=lambda phrase: (-counts[phrase], phrase),
    )[:8]

    covered = len(summaries)
    if files_in_scope and files_in_scope > covered:
        lines = [
            f"recordings summarised here: {covered} of {files_in_scope} in scope "
            f"(the other {files_in_scope - covered} have no digest available)"
        ]
    else:
        lines = [f"recordings: {covered}"]
    if dates:
        lines.append(
            f"dates: {dates[0]} to {dates[-1]}" if dates[0] != dates[-1] else f"date: {dates[0]}"
        )
    if total_seconds > 0:
        lines.append(f"total duration: {_clock(total_seconds)}")
    if roster:
        shown = list(roster)[:12]
        more = f", +{len(roster) - len(shown)} more" if len(roster) > len(shown) else ""
        lines.append(f"speakers ({len(roster)}): {', '.join(shown)}{more}")
    if recurring:
        lines.append(f"recurring topics: {', '.join(recurring)}")
    return lines


def _speaker_focus_header(
    speaker_focus: str,
    summaries: list[FileSummary],
    files_in_scope: int = 0,  # noqa: ARG001
) -> list[str]:
    """The speaker-focus header: talk time / turns / longest monologue.

    W2.3. Also the "never a silent zero" line for a speaker-scoped map: a file
    whose roster names the focus speaker but whose digest contributed no
    stats/content for them gets an explicit coverage note here, rather than
    the whole answer just being short with no explanation. ``files_in_scope``
    is accepted (unused) for signature symmetry with :func:`_corpus_header`.
    """
    from app.services.chat.prompting import _sanitize_body_text

    safe_name = _sanitize_body_text(speaker_focus)
    lines = [f"focus speaker: {safe_name}"]

    with_stats: list[dict[str, Any]] = []
    for summary in summaries:
        if summary.speaker_stats is not None:
            with_stats.append(summary.speaker_stats)
    if with_stats:
        total_seconds = sum(float(stats.get("total_time") or 0.0) for stats in with_stats)
        total_turns = sum(int(stats.get("turn_count") or 0) for stats in with_stats)
        longest = max(float(stats.get("longest_turn") or 0.0) for stats in with_stats)
        lines.append(
            f"talk time across {len(with_stats)} recording(s) with stats: "
            f"{_clock(total_seconds)}, {total_turns} turns, "
            f"longest single turn {_clock(longest)}"
        )

    # A file whose roster names this speaker but whose digest carries neither
    # stats nor content for them: the extractive digest never selected one of
    # their sentences, OR the row predates a speaker rename — facts/digest are
    # regenerated TOGETHER on a fingerprint change, so a genuinely stale row
    # would still name the OLD label and simply would not match `roster` here
    # at all, which is why this note names both possibilities rather than
    # asserting either.
    uncovered = [
        s for s in summaries if s.speaker_in_roster and not s.speaker_stats and not s.digest
    ]
    if uncovered:
        lines.append(
            f"{len(uncovered)} recording(s) list {safe_name} in the roster but have no "
            "matching content here (the digest may not have selected their sentences, "
            "or the digest may predate a speaker rename)"
        )
    return lines


def _empty_speaker_focus_overview(reducer_name: str, speaker_focus: str) -> Overview:
    """Never a silent zero: no file in scope matched the focus speaker at all."""
    from app.services.chat.prompting import _sanitize_body_text

    safe_name = _sanitize_body_text(speaker_focus)
    block = (
        _OVERVIEW_OPEN
        + f"focus speaker: {safe_name}\n"
        + "no recording in scope has digest content attributed to this speaker.\n"
        + _OVERVIEW_CLOSE
    )
    return Overview(block=block, reducer=reducer_name)


class CodeComposer:
    """The NO-LLM reducer. Renders the collection view in code (**D6**).

    First class, not a fallback: the `LLM_PROVIDER`-empty deployment gets a real
    answer to "summarize this collection" from this path, and every fact in it is
    exact because it was counted rather than generated.
    """

    name = "code"

    def reduce(
        self,
        question: str,
        summaries: list[FileSummary],
        files_in_scope: int = 0,
        *,
        speaker_focus: str | None = None,
        **_kwargs,
    ) -> Overview:  # noqa: ARG002
        if not summaries:
            if speaker_focus:
                return _empty_speaker_focus_overview(self.name, speaker_focus)
            return Overview(reducer=self.name)
        from app.services.chat.prompting import _sanitize_attribute
        from app.services.chat.prompting import _sanitize_body_text

        lines = (
            list(_speaker_focus_header(speaker_focus, summaries, files_in_scope))
            if (speaker_focus)
            else []
        )
        if lines:
            lines.append("")
        lines.extend(_corpus_header(summaries, files_in_scope))
        listed = summaries[:MAX_LISTED_FILES]
        if listed:
            lines.append("")
            for summary in listed:
                title = _sanitize_attribute(summary.title) or "Untitled recording"
                date = f" ({summary.recorded_at})" if summary.recorded_at else ""
                lines.append(f"- {title}{date}")
                if summary.digest:
                    # BODY-safe, not the 120-char attribute sanitizer: a digest is
                    # prose, not a short discrete value, and the attribute cap
                    # silently shredded it mid-sentence for anything longer than
                    # a title. `_sanitize_body_text` defuses the same breakout
                    # attempts with no length cap — see its docstring and the
                    # module docstring's "Assembly is concatenation-only" note.
                    lines.append(f"  {_sanitize_body_text(summary.digest)}")
        hidden = len(summaries) - len(listed)
        if hidden > 0:
            lines.append(
                f"({hidden} further recordings are in scope and counted above but not "
                "listed individually here.)"
            )
        return Overview(
            block=_OVERVIEW_OPEN + "\n".join(lines) + "\n" + _OVERVIEW_CLOSE,
            reducer=self.name,
            files_total=len(summaries),
            files_in_scope=files_in_scope or len(summaries),
            files_listed=len(listed),
            truncated=hidden > 0,
        )


_BATCH_SYSTEM = (
    "You are condensing summaries of several recordings into a shorter briefing.\n"
    "For EACH recording, keep its title and one sentence of what it covered. Keep "
    "every recording — never drop one, never merge two.\n"
    "Use only the material given. Add nothing. No preamble."
)


class BatchReducer:
    """The LLM reducer: **many small bounded calls**, never one impossible one.

    Batches the file summaries, condenses each batch in its own call, and
    concatenates the results in code. The final reduce is the turn's existing
    streaming call — so this adds no third summarization path and the answer
    still streams with working citations.

    Falls back to :class:`CodeComposer` on any failure, per batch. A summary that
    silently lost a third of its recordings because one call timed out is the
    failure this whole tier exists to remove, so a failed batch contributes its
    code-composed form rather than nothing.
    """

    name = "llm-batch"

    def __init__(self, llm, *, batch_files: int = DEFAULT_BATCH_FILES) -> None:
        self.llm = llm
        self.batch_files = max(1, int(batch_files))

    def reduce(
        self,
        question: str,
        summaries: list[FileSummary],
        files_in_scope: int = 0,
        *,
        speaker_focus: str | None = None,
        **_kwargs,
    ) -> Overview:
        if not summaries:
            # Preserves the exact pre-W2.3 shape (`reducer == self.name`) when
            # there is no speaker focus; only a speaker-scoped empty result
            # needs the "never a silent zero" note, which is CodeComposer's.
            if speaker_focus:
                return _empty_speaker_focus_overview(self.name, speaker_focus)
            return Overview(reducer=self.name)
        composer = CodeComposer()
        if self.llm is None:
            return composer.reduce(question, summaries, files_in_scope, speaker_focus=speaker_focus)

        batches = [
            summaries[i : i + self.batch_files] for i in range(0, len(summaries), self.batch_files)
        ]
        capped = batches[:MAX_REDUCE_CALLS]
        lines = (
            list(_speaker_focus_header(speaker_focus, summaries, files_in_scope))
            if (speaker_focus)
            else []
        )
        if lines:
            lines.append("")
        lines.extend(_corpus_header(summaries, files_in_scope))
        lines.append("")

        calls = 0
        failures = 0
        for batch in capped:
            rendered = self._condense(batch)
            if rendered is None:
                failures += 1
                rendered = self._plain(batch)
            else:
                calls += 1
            lines.append(rendered)

        covered = sum(len(batch) for batch in capped)
        hidden = len(summaries) - covered
        if hidden > 0:
            lines.append(
                f"({hidden} further recordings are in scope and counted above but were "
                f"not condensed: the {MAX_REDUCE_CALLS}-call ceiling was reached.)"
            )
        return Overview(
            block=_OVERVIEW_OPEN + "\n".join(lines) + "\n" + _OVERVIEW_CLOSE,
            reducer=self.name,
            files_total=len(summaries),
            files_in_scope=files_in_scope or len(summaries),
            files_listed=covered,
            llm_calls=calls,
            truncated=hidden > 0,
            diagnostics={"batches": len(capped), "batch_failures": failures},
        )

    def _plain(self, batch: list[FileSummary]) -> str:
        from app.services.chat.prompting import _sanitize_attribute

        out = []
        for summary in batch:
            title = _sanitize_attribute(summary.title) or "Untitled recording"
            date = f" ({summary.recorded_at})" if summary.recorded_at else ""
            out.append(f"- {title}{date}")
            if summary.digest:
                out.append(f"  {_sanitize_attribute(summary.digest)}")
        return "\n".join(out)

    def _condense(self, batch: list[FileSummary]) -> str | None:
        """One bounded call over one batch. ``None`` on any failure."""
        payload = self._plain(batch)
        messages = [
            {"role": "system", "content": _BATCH_SYSTEM},
            # Concatenation only — titles and digests are untrusted text.
            {"role": "user", "content": "Recordings:\n" + payload},
        ]
        try:
            response = self.llm.chat_completion(messages, max_tokens=400, temperature=0.1)
        except Exception as exc:  # noqa: BLE001 — one failed batch must not lose the rest
            logger.info(f"Overview batch condense failed, using the composed form: {exc}")
            return None
        text = str(getattr(response, "content", "") or "").strip()
        return text or None


def build_overview(
    question: str,
    summaries: list[FileSummary],
    *,
    llm=None,
    use_llm: bool = False,
    batch_files: int = DEFAULT_BATCH_FILES,
    files_in_scope: int = 0,
    speaker_focus: str | None = None,
) -> Overview:
    """Reduce file summaries to one collection view.

    Args:
        question: The user's question. Carried for the reducer interface; the
            code composer deliberately ignores it, because every fact it renders
            is true of the scope regardless of what was asked.
        summaries: The map output.
        llm: The caller's ``LLMService``, or None.
        use_llm: Whether to spend calls. ``False`` — the default — is a complete
            answer, not a degraded one (**D6**).
        batch_files: Recordings per call.
        files_in_scope: How many recordings the user's scope contains. When it
            exceeds what the map covered, the block says so instead of reporting
            the covered count as the total.
        speaker_focus: W2.3. A single active focus speaker's canonical name,
            when this overview was built from ``scope_speaker_digest_hits``.
            Renders a talk-time header and the "never a silent zero" coverage
            notes; ``None`` reproduces the pre-W2.3 block exactly.

    Returns:
        An :class:`Overview`. Empty ``block`` when there is nothing to summarise.
    """
    reducer = BatchReducer(llm, batch_files=batch_files) if (use_llm and llm) else CodeComposer()
    return reducer.reduce(question, summaries, files_in_scope, speaker_focus=speaker_focus)
