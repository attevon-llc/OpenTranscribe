"""POST /files/management/bulk-action caps the size of one request (concurrency audit M10).

The endpoint loops over ``file_uuids`` synchronously inside one request, holding an API
thread and a database connection for every file in the list; with no bound a single
request could keep them for minutes. The cap matches the other bulk schemas (500: holds,
watch-source imports, topic runs). The SPA splits a larger selection into batches
(``frontend/src/lib/api/bulkActions.ts``).
"""

from __future__ import annotations

import uuid

from fastapi import status

#: The cap, the same value as the other bulk file schemas (``MAX_FILES_PER_HOLD``).
BULK_ACTION_MAX_FILES = 500


def _post(client, headers, count: int):
    return client.post(
        "/api/files/management/bulk-action",
        headers=headers,
        json={"file_uuids": [str(uuid.uuid4()) for _ in range(count)], "action": "retry"},
    )


def test_a_request_over_the_cap_is_refused_before_any_file_is_processed(
    client, user_token_headers, monkeypatch
):
    processed: list[str] = []
    monkeypatch.setattr(
        "app.api.endpoints.files.bulk_actions._process_single_file_action",
        lambda db, file_uuid, *a, **k: processed.append(file_uuid),
    )

    response = _post(client, user_token_headers, BULK_ACTION_MAX_FILES + 1)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert processed == []


def test_a_request_at_the_cap_is_processed(client, user_token_headers, monkeypatch):
    from app.api.endpoints.files.bulk_actions import BulkActionResult

    processed: list[str] = []

    def _record(db, file_uuid, *a, **k):
        processed.append(file_uuid)
        return BulkActionResult(file_uuid=file_uuid, success=True, message="ok")

    monkeypatch.setattr("app.api.endpoints.files.bulk_actions._process_single_file_action", _record)

    response = _post(client, user_token_headers, BULK_ACTION_MAX_FILES)

    assert response.status_code == status.HTTP_200_OK
    assert len(processed) == BULK_ACTION_MAX_FILES
    assert len(response.json()) == BULK_ACTION_MAX_FILES


def test_the_cap_matches_the_other_bulk_file_schemas():
    from app.api.endpoints.files import bulk_actions
    from app.schemas.content_hold import MAX_FILES_PER_HOLD

    assert bulk_actions.BULK_ACTION_MAX_FILES == BULK_ACTION_MAX_FILES == MAX_FILES_PER_HOLD
