"""One in-place document rewrite: the ``update_by_query`` call the stamping paths share.

Used by the tenant backfill (``tasks/tenant_backfill_task``, which logs and counts a failure)
and by the trash availability stamp (``search/availability_stamp``, which must SEE one so it
can schedule a retry). Both rewrite fields in place, so both want the same three options
(OpenSearch docs, "Update by query"):

* ``refresh=True`` - the rewritten documents are searchable when the call returns, so a
  read right after a trash or restore sees the new state;
* ``conflicts="proceed"`` - a document changed concurrently is counted in
  ``version_conflicts`` and skipped instead of aborting the whole request;
* a painless ``script`` over ``ctx._source``.

The response is returned whole: ``updated``, ``version_conflicts`` and ``failures`` are the
caller's to judge. A transport error raises.
"""

from __future__ import annotations

from typing import Any


def update_by_query(
    client: Any, index_name: str, query: dict[str, Any], script: dict[str, Any]
) -> dict[str, Any]:
    """Run one refreshed, conflict-tolerant ``update_by_query``; return the raw response."""
    response: dict[str, Any] = client.update_by_query(
        index=index_name,
        refresh=True,
        conflicts="proceed",
        body={"query": query, "script": script},
    )
    return response
