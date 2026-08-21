"""Shared sizing constants and the ``Overview`` result type.

Split out of the former single-file ``mapreduce.py`` (issue tracked in this
package's parent CLAUDE.md — the file had grown to 1242 lines against the
~300-line guideline). This module holds the pieces every other submodule in
the package needs: the tuning constants and the dataclass the reducers
(``reducers.py``) return.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Recordings listed individually before the block starts eliding. The corpus
#: header above them is always complete, so an elided list still reports the true
#: total — a partial list read as complete is the silent-wrong-answer shape this
#: whole stage exists to remove.
MAX_LISTED_FILES = 25

#: Files per LLM call in :class:`~app.services.chat.mapreduce.reducers.BatchReducer`.
#: Small on purpose: "many small fast quick calls" is the design, and a batch
#: large enough to be slow is a batch large enough to be truncated.
DEFAULT_BATCH_FILES = 8

#: Hard ceiling on reduce calls for one turn, whatever the scope. 500 files at 8
#: per batch is 63 calls; this caps the bill and the latency at something a chat
#: turn can survive, and the block says when it bit.
MAX_REDUCE_CALLS = 12

#: Rough character budget the scope map targets when deciding how many leading
#: digest sections to pull PER FILE (``file_summaries.scope_digest_hits``'s
#: ``sections_per_file``). The REAL excerpt budget is only known once the
#: model's context window and reply-token reservation are resolved
#: (``prompting.build_messages``), which runs far downstream of the map step —
#: this is a coarse pre-budget so a scope of many files does not fetch three
#: sections each only to have most of them trimmed away later by
#: ``prompting._trim_evidence_blocks``.
DEFAULT_MAP_BUDGET_CHARS = 12000

_OVERVIEW_OPEN = "<overview>\n"
_OVERVIEW_CLOSE = "</overview>\n\n"


def sections_budget(files: int, budget_chars: int = DEFAULT_MAP_BUDGET_CHARS) -> int:
    """How many leading digest sections per file the scope map should pull.

    ``max(1, min(3, budget_chars // files))``: never less than one section —
    every file in a bounded scope must contribute something to the map — and
    never more than three, so a small scope's per-file allowance cannot balloon
    unbounded. Shrinks as the scope grows, so a 25-file "summarize everything"
    turn does not fetch three sections apiece only to see most of them dropped
    by the excerpt-budget trim that runs later in the pipeline.

    Args:
        files: Number of files in the resolved scope. Zero or negative is
            treated as one, so a caller need not special-case an empty scope.
        budget_chars: The coarse pre-budget. Defaults to
            :data:`DEFAULT_MAP_BUDGET_CHARS`.

    Returns:
        An integer in ``[1, 3]``.
    """
    return max(1, min(3, budget_chars // max(1, files)))


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    return f"{hours}h {remainder // 60:02d}m" if hours else f"{remainder // 60}m"


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
    #: #532 arm (a), EXPERIMENT — ``(citation_id, file_uuid)`` per listed file,
    #: populated only when the composer was given a ``citation_start``. Empty
    #: means the block carries no ids (today's shipped behaviour). Never in
    #: ``as_metadata()``: ids are prompt bookkeeping, not a count.
    cited_entries: tuple[tuple[int, str], ...] = ()
    #: The rendered citation payloads for ``cited_entries``, filled by
    #: ``_prepare_context`` (where the FileSummary list is in scope) and read
    #: by the streaming half when extending ``offered_citations``. Carries
    #: snippets, so it must NEVER enter ``as_metadata()`` — that dict persists.
    citation_payloads: tuple[dict, ...] = ()

    def as_metadata(self) -> dict[str, Any]:
        """Counts only, never content — the same rule the rest of chat follows.

        ``files_in_scope`` was computed on this dataclass from the start (see the
        field's own docstring — it is what lets the block say "8 of 25" instead of
        just "8") but was never included here, so nothing downstream of a chat
        turn's ``meta`` dict — the frontend, or an eval-harness reader like
        ``tests/eval/harness/chat_instrumentation.py`` — could recover the scope
        size to compute a coverage ratio against ``files_total``, only the
        pre-rendered English sentence inside ``block``. Exposing the field that
        already existed, not adding one.
        """
        return {
            "reducer": self.reducer,
            "files_total": self.files_total,
            "files_in_scope": self.files_in_scope,
            "files_listed": self.files_listed,
            "llm_calls": self.llm_calls,
            "truncated": self.truncated,
            **self.diagnostics,
        }
