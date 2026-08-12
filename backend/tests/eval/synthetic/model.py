"""Shared value types for query planning.

Split out of ``queries.py`` so the aggregation rules (``aggregation.py``) can build a
:class:`PlannedQuery` without an import cycle. The previous workaround — passing the class
object in as a ``cls: type`` parameter — defeated type checking on the one dataclass whose
fields *are* the published ground-truth contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass
class PlannedQuery:
    """One evaluation query with its constructed ground truth."""

    query_id: str
    query_class: str
    surface: str
    rule: str
    text: str
    gold_files: list[str]
    answer: object
    answer_kind: str
    #: ``retrieval`` = scored with nDCG/recall against ``gold_files`` / ``gold_turns``.
    #: ``answer`` = scored by exact match on ``answer``; #403 requires the aggregation
    #: class to be answered from OpenSearch aggs or Postgres, not from ranked retrieval,
    #: so scoring it with a ranking metric would measure the wrong path.
    scored_on: str = "retrieval"
    #: Files that legitimately contain the query's subject but are excluded from
    #: ``gold_files`` by a filter in the question (currently only the temporal window).
    #: Published so the reviewer can see the false-negative risk was handled, not hidden.
    related_files: list[str] = field(default_factory=list)
    team_id: str | None = None
    series_id: str | None = None
    topic: str | None = None
    components: list[dict] = field(default_factory=list)
    gold_turns: dict[str, list[list[int]]] = field(default_factory=dict)
