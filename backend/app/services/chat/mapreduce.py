"""Map-reduce over digests — the "1000 transcripts" path (#403 Stage 4, Phase 4).

The problem in the owner's words: *"If I have 1000 transcripts and someone says
'give me a summary of all the transcripts', that is impossible to feed to an
LLM."* And the shape of the answer, also his: *"it's very rarely that a Claude or
ChatGPT chat session is one single large chat session — it's multiple small fast
quick calls combined into one master result."*

This is the industry-standard **map-reduce / `tree_summarize`** pattern, and the
digest plane it maps over is a **DocumentSummaryIndex**. Named here so the names
travel with the code.

## Two levels, and the first one is already paid for

```
transcript chunks  ──(TextRank, at ingest, NO LLM)──▶  file digest      ← the MAP
file digests       ──(code, or N small bounded calls)─▶  collection view ← the REDUCE
```

Level 1 is `services/ingest_artifacts` and it ran when the file was ingested, so
a summary over 1,000 recordings costs **zero** map-time work. That is the whole
reason the extractive digest was built deterministically: a map step that needed
an LLM would make the corpus-scale case exactly as impossible as it was before.

**Not a recursive tree.** Two levels only, deliberately — RAPTOR-style recursive
clustering is interesting for a corpus with no natural structure, and ours has
one: real meeting and speaker-turn boundaries. Recording it as measured future
work rather than scope.

## Two reducers, one interface, and the no-LLM one is FIRST CLASS

:class:`CodeComposer` renders the collection view in code — N recordings, date
span, total duration, speaker roster, recurring keyphrases, then per recording a
title, date and its extractive digest. It is not a degraded fallback: **D6** makes
the `LLM_PROVIDER`-empty deployment first class, and this is what gives it an
answer to "summarize this collection" at all.

:class:`BatchReducer` spends an LLM, in **many small bounded calls** rather than
one impossible one — the owner's framing, implemented literally.

## What it produces, and why that is not a third summarization path

Both reducers return an ``<overview>`` **context block**, not an answer. The turn's
existing streaming call is the final reduce, which means:

* one summarization path, not three — `media_file.summary_data` (the file page)
  is untouched, and the retired `transcript_summaries` index (#67) is not read or
  written here;
* the answer still streams, and `[n]` citations still resolve, because the
  reduced chunk leg runs alongside;
* the model composes the master result, which is the job it is good at.

## Assembly is concatenation-only

Same rule as `prompting.py` and for the same reason: a recording title
containing ``{evil}`` would raise or interpolate under `str.format`. Every value
that reaches a prompt here goes through `prompting._sanitize_attribute` first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

logger = logging.getLogger(__name__)

#: Recordings listed individually before the block starts eliding. The corpus
#: header above them is always complete, so an elided list still reports the true
#: total — a partial list read as complete is the silent-wrong-answer shape this
#: whole stage exists to remove.
MAX_LISTED_FILES = 25

#: Files per LLM call in :class:`BatchReducer`. Small on purpose: "many small
#: fast quick calls" is the design, and a batch large enough to be slow is a
#: batch large enough to be truncated.
DEFAULT_BATCH_FILES = 8

#: Hard ceiling on reduce calls for one turn, whatever the scope. 500 files at 8
#: per batch is 63 calls; this caps the bill and the latency at something a chat
#: turn can survive, and the block says when it bit.
MAX_REDUCE_CALLS = 12

_OVERVIEW_OPEN = "<overview>\n"
_OVERVIEW_CLOSE = "</overview>\n\n"


@dataclass(frozen=True)
class FileSummary:
    """One recording's contribution — the MAP output, read not computed.

    Every field here already exists on disk when a file finishes ingesting. The
    map step is a database read.
    """

    file_uuid: str
    title: str = ""
    recorded_at: str | None = None
    duration: float | None = None
    speakers: tuple[str, ...] = ()
    keyphrases: tuple[str, ...] = ()
    #: The digest text, **already masked** by the caller. This module never sees
    #: raw index content and never masks: masking needs a session and a policy
    #: subject, and a module that quietly did it would be a second place for the
    #: fail-closed contract to drift out of step with `redactor.py`.
    digest: str = ""


@dataclass
class Overview:
    """A rendered collection view, plus what it cost and what it left out."""

    block: str = ""
    reducer: str = ""
    #: Recordings the map actually covered.
    files_total: int = 0
    #: Recordings the user's scope contains. When this exceeds ``files_total``
    #: the block SAYS SO. Reporting the covered count as though it were the scope
    #: is how a summary states "8 sessions" over a scope of 25 — measured, in the
    #: first end-to-end run of this module.
    files_in_scope: int = 0
    files_listed: int = 0
    llm_calls: int = 0
    truncated: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """Counts only, never content — the same rule the rest of chat follows."""
        return {
            "reducer": self.reducer,
            "files_total": self.files_total,
            "files_listed": self.files_listed,
            "llm_calls": self.llm_calls,
            "truncated": self.truncated,
            **self.diagnostics,
        }


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    return f"{hours}h {remainder // 60:02d}m" if hours else f"{remainder // 60}m"


def build_file_summaries(db, digests, *, masked_text: dict[int, str]) -> list[FileSummary]:
    """Group masked digest hits by file and attach each file's stored facts.

    Args:
        db: Session, for the `file_facts` read. ``None`` yields summaries with
            digest text only — degraded but never wrong.
        digests: Digest ``ChunkHit``s, in rank order.
        masked_text: ``id(hit) -> masked content``. Passed in rather than read off
            the hit so this module cannot be handed unmasked text by accident.

    Returns:
        One :class:`FileSummary` per distinct file, in the order the digest leg
        ranked them — best file first, which is the order the block lists them in.
    """
    ordered: dict[str, list] = {}
    for hit in digests:
        ordered.setdefault(str(hit.file_uuid), []).append(hit)
    if not ordered:
        return []

    facts_by_file: dict[str, dict[str, Any]] = {}
    if db is not None:
        facts_by_file = _load_facts(db, [hit[0].file_id for hit in ordered.values()])

    summaries: list[FileSummary] = []
    for file_uuid, hits in ordered.items():
        payload = facts_by_file.get(str(hits[0].file_id), {})
        facts = payload.get("facts") or {}
        keyphrases = payload.get("keyphrases") or {}
        # Sections are joined in the order the digest leg returned them, which is
        # relevance order, not transcript order. Said here because the reader of a
        # summary would reasonably assume chronology.
        text = " ".join(masked_text.get(id(hit), "").strip() for hit in hits).strip()
        summaries.append(
            FileSummary(
                file_uuid=file_uuid,
                title=str(hits[0].title or ""),
                recorded_at=(
                    str(facts.get("recorded_at"))[:10] if facts.get("recorded_at") else None
                ),
                duration=facts.get("duration_seconds"),
                speakers=tuple(str(name) for name in (facts.get("roster") or [])),
                keyphrases=tuple(
                    str(entry.get("phrase", "")) for entry in (keyphrases.get("phrases") or [])[:5]
                ),
                digest=text,
            )
        )
    return summaries


def scope_digest_hits(db, file_uuids: list[str], *, sections_per_file: int = 1) -> list:
    """One digest per file **for every file in scope** — the actual MAP step.

    ⚠️ **This is not the ranked digest leg, and the difference is a measured
    defect, not a preference.** ``retrieve_digests`` returns the top-K digest
    *sections* by relevance, and sections cluster: asked for 50 over a 25-file
    scope it returned 50 sections drawn from **8 files**. Composing an overview
    from that produced a block headed "recordings: 8" and an answer that
    confidently reported *"8 vendor review board sessions"* over a scope of 25.

    The mistake was conflating two different operations. Ranking picks the best
    passages; **mapping covers every document, by definition** — that is what the
    "map" in map-reduce means. So for a bounded scope the map reads
    ``file_facts`` for each file directly and ignores relevance entirely.

    Returns ``ChunkHit``s rather than raw rows so the result goes through the
    *same* ``redactor.mask_digests`` path as the ranked leg. A second masking
    implementation for the same text is how a fail-closed contract drifts.

    Args:
        db: Session.
        file_uuids: The resolved scope. Bounded — an unbounded scope cannot be
            mapped over and must use the ranked leg instead.
        sections_per_file: Leading digest sections per file. One is usually
            enough for a collection view and keeps the block inside its budget.

    Returns:
        ``ChunkHit``s carrying ``digest_section``, in scope order.
    """
    if not file_uuids:
        return []
    from app.models.file_facts import FileFacts
    from app.models.media import MediaFile
    from app.services.search.chunk_retrieval import ChunkHit

    try:
        rows = (
            db.query(MediaFile.id, MediaFile.uuid, MediaFile.title, FileFacts.digest)
            .join(FileFacts, FileFacts.media_file_id == MediaFile.id)
            .filter(MediaFile.uuid.in_(list(file_uuids)))
            .all()
        )
    except Exception:  # noqa: BLE001 — a missing map degrades the answer, never breaks it
        logger.exception("Could not read file_facts for the scope map")
        return []

    hits: list[Any] = []
    for file_id, uuid, title, digest in rows:
        for section in (digest or {}).get("sections", [])[:sections_per_file]:
            hits.append(
                ChunkHit(
                    file_uuid=str(uuid),
                    file_id=int(file_id),
                    chunk_index=-1 - int(section.get("index", 0)),
                    content=str(section.get("text") or ""),
                    title=str(title or ""),
                    start_time=float(section.get("start_time") or 0.0),
                    end_time=section.get("end_time"),
                    digest_section=int(section.get("index", 0)),
                )
            )
    return hits


def _load_facts(db, file_ids: list[int]) -> dict[str, dict[str, Any]]:
    """`str(file_id) -> {facts, keyphrases}` for the files in scope. One query."""
    try:
        from app.models.file_facts import FileFacts

        rows = (
            db.query(FileFacts.media_file_id, FileFacts.facts, FileFacts.keyphrases)
            .filter(FileFacts.media_file_id.in_(list(file_ids)))
            .all()
        )
    except Exception:  # noqa: BLE001 — a summary without facts is degraded, not broken
        logger.exception("Could not load file_facts for the overview; composing without them")
        return {}
    return {str(row[0]): {"facts": row[1] or {}, "keyphrases": row[2] or {}} for row in rows}


def _corpus_header(summaries: list[FileSummary], files_in_scope: int = 0) -> list[str]:
    """The facts that are true of the whole scope, and are exact."""
    dates = sorted(s.recorded_at for s in summaries if s.recorded_at)
    total_seconds = sum(float(s.duration or 0.0) for s in summaries)
    roster: dict[str, None] = {}
    for summary in summaries:
        for name in summary.speakers:
            roster.setdefault(name, None)

    counts: dict[str, int] = {}
    for summary in summaries:
        for phrase in summary.keyphrases:
            counts[phrase] = counts.get(phrase, 0) + 1
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


class CodeComposer:
    """The NO-LLM reducer. Renders the collection view in code (**D6**).

    First class, not a fallback: the `LLM_PROVIDER`-empty deployment gets a real
    answer to "summarize this collection" from this path, and every fact in it is
    exact because it was counted rather than generated.
    """

    name = "code"

    def reduce(
        self, question: str, summaries: list[FileSummary], files_in_scope: int = 0, **_kwargs
    ) -> Overview:  # noqa: ARG002
        if not summaries:
            return Overview(reducer=self.name)
        from app.services.chat.prompting import _sanitize_attribute

        lines = _corpus_header(summaries, files_in_scope)
        listed = summaries[:MAX_LISTED_FILES]
        if listed:
            lines.append("")
            for summary in listed:
                title = _sanitize_attribute(summary.title) or "Untitled recording"
                date = f" ({summary.recorded_at})" if summary.recorded_at else ""
                lines.append(f"- {title}{date}")
                if summary.digest:
                    lines.append(f"  {_sanitize_attribute(summary.digest)}")
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
        self, question: str, summaries: list[FileSummary], files_in_scope: int = 0, **_kwargs
    ) -> Overview:
        if not summaries:
            return Overview(reducer=self.name)
        composer = CodeComposer()
        if self.llm is None:
            return composer.reduce(question, summaries, files_in_scope)

        batches = [
            summaries[i : i + self.batch_files] for i in range(0, len(summaries), self.batch_files)
        ]
        capped = batches[:MAX_REDUCE_CALLS]
        lines = _corpus_header(summaries, files_in_scope)
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

    Returns:
        An :class:`Overview`. Empty ``block`` when there is nothing to summarise.
    """
    reducer = BatchReducer(llm, batch_files=batch_files) if (use_llm and llm) else CodeComposer()
    return reducer.reduce(question, summaries, files_in_scope)
