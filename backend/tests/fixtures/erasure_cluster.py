"""A stand-in OpenSearch cluster whose answer to each erasure API call the test chooses.

Used by ``test_org_admin_gdpr.py``. Unlike ``speaker_family_cluster.py``, which evaluates what
it is asked against stored documents, this one is configured per test: how many documents a
``delete_by_query`` reports, how many survive per index, and which APIs are unreachable.
That is what the GDPR suites need, because what they test is the app's behaviour when a store
cannot PROVE the subject's documents are gone, not OpenSearch.
"""

from __future__ import annotations

from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError


def _unavailable(api: str) -> OpenSearchConnectionError:
    """The exception opensearch-py raises when the cluster cannot be reached.

    Built with opensearch-py's own three-argument shape (``status``, ``error``,
    ``info``) because ``ConnectionError.__str__`` reads ``args[1]`` and
    ``args[2]`` — a one-argument construction blows up on ``str(e)``, in the
    error-reporting code the test exists to exercise.
    """
    return OpenSearchConnectionError("N/A", f"{api}: connection refused", Exception("refused"))


class _Indices:
    def __init__(self, cluster: ErasureCluster):
        self._cluster = cluster

    def exists(self, index: str) -> bool:
        self._cluster.maybe_fail("indices.exists")
        return index not in self._cluster.absent_indices

    def exists_alias(self, name: str) -> bool:
        self._cluster.maybe_fail("indices.exists")
        return False

    def refresh(self, index: str, ignore_unavailable: bool = False) -> None:
        """A ``count`` is a search and sees only what the last refresh made visible.

        The chunks-index survivor count forces one first (#435), so unrefreshed
        survivors of a delete that failed part-way cannot read as gone. Recorded
        rather than ignored so a test can assert it happened.
        """
        self._cluster.maybe_fail("indices.refresh")
        self._cluster.refreshed.append(index)


class ErasureCluster:
    """A stand-in cluster whose answer to each API call the test chooses.

    OpenSearch itself is not the system under test — the app's behaviour when a
    store cannot *prove* the subject's documents are gone is. So the erasure
    functions run unmodified and talk to this.

    Args:
        deleted: ``deleted`` count each ``delete_by_query`` reports.
        counts: Per-index survivor counts returned by ``count`` (default 0).
        failures: ``failures`` list each ``delete_by_query`` reports — the way
            OpenSearch reports a *partial* sweep, in the body, without raising.
        fail: API names (``delete``, ``delete_by_query``, ``count``,
            ``indices.exists``, ``indices.refresh``) that raise "cluster
            unreachable" instead. The speaker-family erasure deletes in one
            ``_bulk`` request and verifies with one aggregation ``search``; those
            answer to ``delete`` and ``count``, the operations they perform.
        absent_indices: Index names ``indices.exists`` answers False for.
    """

    def __init__(self, *, deleted=0, counts=None, failures=None, fail=(), absent_indices=()):
        self.deleted = deleted
        self.counts = counts or {}
        self.failures = list(failures or [])
        self.fail = set(fail)
        self.absent_indices = set(absent_indices)
        self.indices = _Indices(self)
        self.deleted_docs: list[tuple[str, str]] = []
        self.delete_by_query_calls: list[tuple[str, dict]] = []
        self.refreshed: list[str] = []

    def maybe_fail(self, api: str) -> None:
        if api in self.fail:
            raise _unavailable(api)

    def delete(self, index, id):  # noqa: A002 — opensearch-py's parameter name
        self.maybe_fail("delete")
        self.deleted_docs.append((index, str(id)))
        return {"result": "deleted"}

    def delete_by_query(self, index, body, refresh=False, conflicts=None):
        self.maybe_fail("delete_by_query")
        self.delete_by_query_calls.append((index, body))
        return {"deleted": self.deleted, "failures": self.failures}

    def count(self, index, body=None):
        self.maybe_fail("count")
        return {"count": self.counts.get(index, 0)}

    def bulk(self, body):
        """By-id deletes in one request (the speaker family's voiceprints and embeddings)."""
        self.maybe_fail("delete")
        items = []
        for action in body:
            ((op, meta),) = action.items()
            self.deleted_docs.append((meta["_index"], str(meta["_id"])))
            items.append({op: {"_index": meta["_index"], "_id": meta["_id"], "status": 200}})
        return {"errors": False, "items": items}

    def search(self, index, body, ignore_unavailable=False):
        """The speaker-family survivor read: ``counts`` per concrete index, by aggregation."""
        self.maybe_fail("count")
        buckets = [
            {"key": idx, "doc_count": self.counts[idx]}
            for idx in index.split(",")
            if self.counts.get(idx) and idx not in self.absent_indices
        ]
        return {"aggregations": {"per_index": {"buckets": buckets}}}
