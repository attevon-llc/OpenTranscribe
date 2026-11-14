"""A stand-in OpenSearch cluster that answers the erasers by evaluating what it is asked.

Models the behaviour the speaker-family erasure relies on, so a test fails on a wrong
document set rather than on a different request body:

* deletes by id (single or ``_bulk``) are realtime, but a search or ``count`` reads a
  SEARCHER, which keeps listing a deleted document until ``indices.refresh``;
* a ``_bulk`` delete never creates its index: a missing index or document answers that
  item 404, and a write-blocked index answers 403 and keeps the document;
* a multi-target ``a,b`` request resolves an alias to its concrete index, counts each
  concrete index once, and skips a missing target only with ``ignore_unavailable``;
* a ``delete_by_query`` whose scroll meets a document already deleted by id answers 409
  unless it was sent ``conflicts="proceed"``.

Every request is recorded in ``calls`` as ``(method, target, ...)``, so a test can bound
how many round trips an operation costs.
"""

from __future__ import annotations

from typing import Any

from opensearchpy.exceptions import ConflictError
from opensearchpy.exceptions import NotFoundError


def matches(doc_id: str, doc: dict[str, Any], query: dict[str, Any]) -> bool:
    if "ids" in query:
        return doc_id in query["ids"]["values"]
    if "term" in query:
        ((field, value),) = query["term"].items()
        return bool(doc.get(field) == value)
    if "terms" in query:
        ((field, values),) = query["terms"].items()
        return doc.get(field) in values
    if "match_all" in query:
        return True
    raise AssertionError(f"the stand-in cannot evaluate {query!r}; teach it, don't skip it")


class _Indices:
    def __init__(self, cluster: SpeakerFamilyCluster) -> None:
        self._cluster = cluster

    def exists(self, index: str) -> bool:
        self._cluster.calls.append(("exists", index))
        return index in self._cluster.docs or index in self._cluster.aliases

    def refresh(self, index: str, ignore_unavailable: bool = False) -> None:
        self._cluster.calls.append(("refresh", index))
        for concrete in self._cluster.resolve(index, ignore_unavailable):
            self._cluster.searcher[concrete] = dict(self._cluster.docs[concrete])


class SpeakerFamilyCluster:
    """Live documents per index, plus the searcher's (stale until refresh) view of them."""

    def __init__(
        self,
        docs: dict[str, dict[str, dict[str, Any]]],
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.docs = {index: dict(found) for index, found in docs.items()}
        self.searcher = {index: dict(found) for index, found in docs.items()}
        self.aliases = dict(aliases or {})
        self.write_blocked: set[str] = set()
        self.indices = _Indices(self)
        self.calls: list[tuple[Any, ...]] = []
        self.failures: list[dict[str, Any]] = []

    # -- helpers ---------------------------------------------------------------

    def resolve(self, targets: str, ignore_unavailable: bool = False) -> list[str]:
        """Concrete indices behind a comma-separated target list, each once."""
        out: list[str] = []
        for name in targets.split(","):
            concrete = self.aliases.get(name, name)
            if concrete not in self.docs:
                if ignore_unavailable:
                    continue
                raise NotFoundError(404, "index_not_found_exception", {"index": name})
            if concrete not in out:
                out.append(concrete)
        return out

    def delete_now(self, index: str, doc_id: str) -> None:
        """A realtime by-id delete: gone at once, still listed by the searcher."""
        self.docs.get(index, {}).pop(doc_id, None)

    def requests(self, *methods: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if not methods or c[0] in methods]

    # -- document APIs ---------------------------------------------------------

    def delete(self, index: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete", index))
        concrete = self.aliases.get(index, index)
        doc_id = kwargs["id"]
        if doc_id not in self.docs.get(concrete, {}):
            raise NotFoundError(404, "not_found", {})
        self.docs[concrete].pop(doc_id)
        return {"result": "deleted"}

    def bulk(self, body: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("bulk", len(body)))
        items = []
        for action in body:
            ((op, meta),) = action.items()
            assert op == "delete", f"the stand-in only models bulk deletes, got {op!r}"
            index = self.aliases.get(meta["_index"], meta["_index"])
            if index not in self.docs:
                status, error = 404, {"type": "index_not_found_exception"}
            elif index in self.write_blocked:
                status, error = 403, {"type": "cluster_block_exception"}
            elif meta["_id"] not in self.docs[index]:
                status, error = 404, None
            else:
                self.docs[index].pop(meta["_id"])
                status, error = 200, None
            item: dict[str, Any] = {"_index": index, "_id": meta["_id"], "status": status}
            if error:
                item["error"] = error
            items.append({op: item})
        return {"errors": any(i["delete"]["status"] >= 400 for i in items), "items": items}

    def count(self, index: str, body: dict[str, Any]) -> dict[str, int]:
        self.calls.append(("count", index))
        if index not in self.docs:
            raise NotFoundError(404, "index_not_found_exception", {})
        found = self.searcher.get(index, {})
        return {"count": sum(1 for i, d in found.items() if matches(i, d, body["query"]))}

    def search(
        self, index: str, body: dict[str, Any], ignore_unavailable: bool = False
    ) -> dict[str, Any]:
        self.calls.append(("search", index))
        assert body.get("size") == 0, "the survivor read needs counts, not hits"
        agg = body["aggs"]["per_index"]["terms"]
        assert agg["field"] == "_index", agg
        buckets = []
        for concrete in self.resolve(index, ignore_unavailable):
            n = sum(
                1
                for doc_id, doc in self.searcher.get(concrete, {}).items()
                if matches(doc_id, doc, body["query"])
            )
            if n:
                buckets.append({"key": concrete, "doc_count": n})
        return {"aggregations": {"per_index": {"buckets": buckets[: agg["size"]]}}}

    def delete_by_query(self, index: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_by_query", index, body["query"], kwargs))
        query = body["query"]
        listed = [i for i, d in self.searcher.get(index, {}).items() if matches(i, d, query)]
        live = self.docs.get(index, {})
        if kwargs.get("conflicts") != "proceed" and any(i not in live for i in listed):
            raise ConflictError(409, "version_conflict_engine_exception", {})
        for doc_id in listed:
            live.pop(doc_id, None)
        if kwargs.get("refresh"):
            self.searcher[index] = dict(live)
        return {"deleted": len(listed), "failures": self.failures}
