"""Scanner 4's remediation, proved at the HTTP boundary (#914 container-taint).

Sibling of ``test_error_detail_sanitization.py``, for the shape that gate could not
see: a caught exception's text written into a CONTAINER that the endpoint returns.

Every test asserts the same THREE things that file does — the status code, that a
SENTINEL planted in the mocked failure is ABSENT from the response body, and that it
IS present in ``caplog``. A test that only checked the status would pass against the
broken code and prove nothing; a test that only checked the sentinel's absence would
pass against an endpoint that had stopped reporting the failure at all.

Each also asserts that the SAFE half of the report survived — the per-file identity in
a bulk action, the presence of the diagnostic key in the debug report. Dropping those
would "fix" the leak by deleting a real capability.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import status

SENTINEL = "SENTINEL-c0nt41n3r-redis://:hunter2@broker-internal:6379/0"


def test_bulk_action_per_file_failure_does_not_echo_the_exception(
    client, user_token_headers, normal_user, monkeypatch, caplog
):
    """``results`` IS the response body of POST /api/files/management/bulk-action.

    The per-file ``BulkActionResult`` used to carry ``f"Unexpected error: {str(e)}"``,
    and a bulk action's handlers reach MinIO, Celery and the filesystem — so that
    string could be a broker URL with its password, as planted here.
    """
    file_uuid = str(uuid.uuid4())

    def _raise(*args, **kwargs):
        raise RuntimeError(f"could not dispatch: {SENTINEL}")

    monkeypatch.setattr("app.api.endpoints.files.management._process_single_file_action", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/files/management/bulk-action",
            headers=user_token_headers,
            json={"file_uuids": [file_uuid], "action": "retry"},
        )

    assert response.status_code == status.HTTP_200_OK
    body_text = response.text
    assert SENTINEL not in body_text
    assert "hunter2" not in body_text

    # The capability a bulk report exists for is intact: WHICH file failed, that it
    # failed, and a machine-readable code the SPA can branch on.
    (result,) = response.json()
    assert result["file_uuid"] == file_uuid
    assert result["success"] is False
    assert result["error"] == "UNEXPECTED_ERROR"
    assert result["message"] == "Unexpected error (RuntimeError)"

    # Still diagnosable.
    assert SENTINEL in caplog.text


def test_speaker_debug_report_does_not_echo_the_opensearch_exception(
    client, admin_token_headers, monkeypatch, caplog
):
    """``section`` is merged into the debug endpoint's response body.

    ``section["opensearch_error"] = str(e)`` returned the opensearch-py exception
    verbatim, which quotes the cluster URL and the failing request.
    """

    class _ExplodingClient:
        def search(self, *args, **kwargs):
            raise RuntimeError(f"connection refused: {SENTINEL}")

    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", _ExplodingClient())

    with caplog.at_level(logging.ERROR):
        response = client.get("/api/speakers/debug/cross-media-data", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body_text = response.text
    assert SENTINEL not in body_text
    assert "hunter2" not in body_text

    # The diagnostic contract survives: the endpoint still REPORTS that the
    # OpenSearch half is unavailable rather than silently returning a partial
    # document that looks complete.
    payload = response.json()
    assert payload["opensearch_error"] == "OpenSearch section unavailable (RuntimeError)"

    assert SENTINEL in caplog.text
