"""The adapter contract every eval corpus implements.

An adapter's whole job is: given a directory on disk, yield
:class:`~..model.MeetingDoc` objects. It never touches the database, Celery or
OpenSearch — the injection core owns all of that — so adding a corpus is a
parsing exercise with unit tests that need no running stack.

Timing responsibility is split deliberately. An adapter fills ``Turn.start`` /
``Turn.end`` **only** where it has a real, measured source, and sets
``MeetingDoc.timing.source`` accordingly. It never invents a timestamp;
:func:`~..timings.resolve_timings` does that, once, in one place, and is what
stamps the synthetic provenance the guard later reads.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from pathlib import Path

from app.scripts.corpus_injection.model import CorpusInfo
from app.scripts.corpus_injection.model import MeetingDoc


class CorpusAdapter(abc.ABC):
    """Parse one third-party corpus into the injection data model."""

    #: Short stable key. Appears in the manifest and in every derived UUID, so
    #: renaming it renumbers the corpus.
    key: str = ""

    #: True when this corpus's gold sets span several files, so truncating
    #: :meth:`meeting_ids` (``--limit``, ``--only``) can leave a query with only
    #: part of its gold set on the stack. The harness *drops* such a query, which
    #: presents as a smaller query count rather than as an error — so the CLI
    #: warns instead of letting the run look complete. An adapter whose gold is
    #: single-file (QMSum) is unaffected and leaves this False.
    subset_breaks_gold_closure: bool = False

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @abc.abstractmethod
    def describe(self) -> CorpusInfo:
        """Identify this copy of the corpus: version, licence tier, on-disk root."""

    @abc.abstractmethod
    def meeting_ids(self) -> list[str]:
        """Every meeting this adapter can produce, in a stable sorted order."""

    @abc.abstractmethod
    def load(self, meeting_id: str) -> MeetingDoc:
        """Parse one meeting. Timings are left unresolved."""

    def meetings(self, only: list[str] | None = None) -> Iterator[MeetingDoc]:
        """Yield meetings in deterministic order, optionally filtered by id."""
        wanted = set(only) if only else None
        for meeting_id in self.meeting_ids():
            if wanted is not None and meeting_id not in wanted:
                continue
            yield self.load(meeting_id)
