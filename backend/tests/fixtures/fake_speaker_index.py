"""An in-memory speaker index for tests of the voiceprint delete paths (erasure and privacy purge).

Evaluates the ``delete_by_query`` shapes those paths send (``match_all``, or a ``bool`` of
``term`` filters and ``exists`` must_nots) against documents, so a wrong scope shows up as a
document that survived or went, not only as a different request body. It raises on a clause it
does not model, so a new query shape cannot slip through as "matched nothing".
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
        self.indices = SimpleNamespace(exists=lambda index: True)

    def add(self, doc_id: str, user_id: int, organization_id: int | None) -> None:
        doc: dict[str, Any] = {"id": doc_id, "user_id": user_id}
        if organization_id is not None:
            doc["organization_id"] = organization_id
        self.docs.append(doc)

    def ids(self) -> set[str]:
        return {d["id"] for d in self.docs}

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "match_all" in query:
            return True
        clause = query["bool"]
        assert set(clause) <= {"filter", "must_not"}, f"unmodelled clause {set(clause)}"
        for f in clause.get("filter", []):
            ((field, value),) = f["term"].items()
            if doc.get(field) != value:
                return False
        return all(n["exists"]["field"] not in doc for n in clause.get("must_not", []))

    def delete_by_query(self, index, body, refresh=True, conflicts="proceed"):
        self.queries.append(body)
        # The same documents sit in every index in this model, so count them once.
        if index != constants.get_speaker_index_v4():
            return {"deleted": 0}
        keep = [d for d in self.docs if not self._matches(d, body["query"])]
        deleted = len(self.docs) - len(keep)
        self.docs = keep
        return {"deleted": deleted, "failures": []}
