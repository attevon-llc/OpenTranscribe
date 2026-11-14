"""Audit rows carry the OpenSearch ``_id`` as ``id`` so the UI can key them (B1)."""

from unittest.mock import MagicMock
from unittest.mock import patch

from app.auth import audit_query


def test_query_audit_logs_returns_hit_id_on_each_row():
    client = MagicMock()
    client.search.return_value = {
        "hits": {
            "hits": [
                {"_id": "a1", "_source": {"event_type": "login"}},
                {"_id": "b2", "_source": {"event_type": "logout"}},
            ],
            "total": {"value": 2},
        }
    }
    with (
        patch.object(audit_query.settings, "AUDIT_LOG_TO_OPENSEARCH", True),
        patch.object(audit_query, "_build_audit_opensearch_client", return_value=client),
    ):
        result = audit_query.query_audit_logs()
    assert [row["id"] for row in result["logs"]] == ["a1", "b2"]
    assert result["logs"][0]["event_type"] == "login"
