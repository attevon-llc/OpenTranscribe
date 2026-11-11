"""Create and remove a test's throwaway OpenSearch index with a per-request timeout.

Index creation is a cluster-state update, and the cluster applies those one at a time: when
many xdist workers create their fixture indexes at once, the last request waits for all the
others. Under a loaded host that wait passed the client's 10 s default read timeout (measured
~14 s at load 38 in the P3.2 gate), so ``indices.create`` raised ``ConnectionTimeout`` while
the cluster went on to create the index. 133 setups errored and the indexes they had already
created leaked, which then kept every erasure receipt ``unverified`` (``os.unclassified``).

Two rules follow, and the teardown-discipline guard
(``unit/test_test_artifact_teardown_discipline.py``) enforces both:

* the creation runs INSIDE the ``try`` whose ``finally`` deletes the index, because a timed-out
  create may still have created it, and the delete tolerates a missing index (404);
* the creation states its own ``request_timeout``. That is opensearch-py's per-request
  transport option (``Transport.perform_request`` pops ``request_timeout`` from the call's
  params ahead of the client default; ``client/utils.py:query_params`` passes it through
  unescaped), the same mechanism ``app.services.opensearch_service.call_idempotent_write`` uses
  for the app's own writes. The client-wide timeout stays at its default, so a slow search
  still fails fast, and ``retry_on_timeout`` stays off: a create is not idempotent, and a
  retry after a timeout that DID create the index would fail with
  ``resource_already_exists_exception``.
"""

from __future__ import annotations

from typing import Any

#: Seconds one test-index create or delete may wait in the cluster-state queue. Matches the
#: wait ``fixtures/summary_plane.py`` measured and documented for the same queue.
TEST_INDEX_REQUEST_TIMEOUT_S = 180


def create_test_index(client: Any, name: str, body: dict[str, Any] | None = None) -> None:
    """Create ``name``. Call it inside the ``try`` whose ``finally`` calls :func:`delete_test_index`."""
    client.indices.create(index=name, body=body, request_timeout=TEST_INDEX_REQUEST_TIMEOUT_S)


def delete_test_index(client: Any, name: str) -> None:
    """Delete ``name`` if it exists (a create that failed outright leaves nothing to delete)."""
    client.indices.delete(index=name, ignore=[404], request_timeout=TEST_INDEX_REQUEST_TIMEOUT_S)
