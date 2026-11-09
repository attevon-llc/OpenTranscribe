"""An outage must not read as a zero document count."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.api.endpoints.search_index_health import _scope_doc_counts_to_caller
from app.models.user import User
from app.services.request_context import RequestContext


def _user() -> User:
    return User(id=7, email="metrics-reader@example.invalid", role="admin")


def test_scoped_doc_count_failure_marks_the_index_unreachable_not_empty():
    health = {"idx": {"status": "green", "doc_count": 40, "error": None}}
    client = MagicMock()
    client.count.side_effect = RuntimeError("cluster down")
    ctx = RequestContext(user=_user(), org_id=None)

    _scope_doc_counts_to_caller(client, health, ctx)

    assert health["idx"]["status"] == "red"
    assert health["idx"]["error"]


def test_scoped_doc_count_success_keeps_the_index_green():
    health = {"idx": {"status": "green", "doc_count": 40, "error": None}}
    client = MagicMock()
    client.count.return_value = {"count": 3}
    ctx = RequestContext(user=_user(), org_id=None)

    _scope_doc_counts_to_caller(client, health, ctx)

    assert health["idx"] == {"status": "green", "doc_count": 3, "error": None}
