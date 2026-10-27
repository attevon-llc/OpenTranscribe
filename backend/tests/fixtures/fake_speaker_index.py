"""An in-memory speaker index for tests of the voiceprint delete paths (erasure and privacy purge).

Evaluates the ``delete_by_query`` / ``count`` shapes those paths send (``match_all``, ``ids``, or
a ``bool`` of ``term`` filters and ``exists`` / ``term`` must_nots) against documents, so a wrong
scope shows up as a document that survived or went, not only as a different request body. It
raises on a clause it does not model, so a new query shape cannot slip through as "matched
nothing".

A document is ``{"id", "user_id"[, "organization_id"][, "document_type"]}``. ``id`` is the
OpenSearch ``_id`` (a speaker uuid, ``profile_<uuid>``, ``cluster_<uuid>``); a document with NO
``organization_id`` is how legacy and personal documents look, which is exactly what the
purge-scope tests need to seed inconsistently.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.core import constants


class FakeOpenSearch:
    """Evaluates the delete_by_query shapes the purge sends against in-memory documents."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []
        self.indices = SimpleNamespace(exists=lambda index: True, refresh=lambda index: {})

    def add(
        self,
        doc_id: str,
        user_id: int,
        organization_id: int | None,
        document_type: str | None = None,
    ) -> None:
        doc: dict[str, Any] = {"id": doc_id, "user_id": user_id}
        if organization_id is not None:
            doc["organization_id"] = organization_id
        if document_type is not None:
            doc["document_type"] = document_type
        self.docs.append(doc)

    def ids(self) -> set[str]:
        return {d["id"] for d in self.docs}

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "match_all" in query:
            return True
        if "ids" in query:
            return doc["id"] in set(query["ids"]["values"])
        clause = query["bool"]
        assert set(clause) <= {"filter", "must_not"}, f"unmodelled clause {set(clause)}"
        for f in clause.get("filter", []):
            ((field, value),) = f["term"].items()
            if doc.get(field) != value:
                return False
        for n in clause.get("must_not", []):
            if "exists" in n:
                if n["exists"]["field"] in doc:
                    return False
            else:
                ((field, value),) = n["term"].items()
                if doc.get(field) == value:
                    return False
        return True

    def delete_by_query(self, index, body, refresh=True, conflicts="proceed"):
        self.queries.append(body)
        # The same documents sit in every index in this model, so count them once.
        if index != constants.get_speaker_index_v4():
            return {"deleted": 0}
        keep = [d for d in self.docs if not self._matches(d, body["query"])]
        deleted = len(self.docs) - len(keep)
        self.docs = keep
        return {"deleted": deleted, "failures": []}

    def count(self, index, body):
        """Documents matching ``body`` once, whichever indices (alias included) are listed."""
        return {"count": sum(1 for d in self.docs if self._matches(d, body["query"]))}
