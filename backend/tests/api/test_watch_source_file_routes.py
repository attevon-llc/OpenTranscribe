"""The per-file routes of ``watch_sources.py`` — list, filter, delete, retry.

These were effectively untested before #489: the existing suite covered the empty
envelope and the ``page_size > 200`` guard, so there was no test with actual rows, no
test of ``?status=``, and none at all of the delete route. Everything the settings
page's file table relies on was unpinned.

**Retry is batch-shaped even for one row.** ``scan_single`` holds a Redis lock per
source, so a per-file endpoint would dispatch one scan per file and every one after
the first would silently no-op. The dispatch is patched here — what these assert is
that exactly ONE is requested per batch, which is the property the shape exists for.

No mail, no network, no MinIO: every row is created directly and no import runs.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile

BASE = "/api/watch-sources"

#: Never inserted. A literal rather than ``uuid4()`` — a parametrize argument is
#: evaluated at import time and becomes part of the test id, so a random one gives each
#: xdist worker a different id and the whole suite fails collection.
ABSENT_UUID = "00000000-0000-4000-8000-0000000000ff"


def _make_source(db_session, owner, **overrides) -> WatchSource:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "user_id": owner.id,
        "created_by": owner.id,
        "name": f"watch-{uuid_pkg.uuid4().hex[:8]}",
        "source_type": "local",
        "is_enabled": True,
        "local_path": f"pytest/{uuid_pkg.uuid4().hex[:8]}",
    }
    defaults.update(overrides)
    source = WatchSource(**defaults)
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


def _make_row(db_session, source, **overrides) -> WatchSourceFile:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "watch_source_id": source.id,
        "remote_path": f"/watch/{uuid_pkg.uuid4().hex}.mp4",
        "filename": f"{uuid_pkg.uuid4().hex[:8]}.mp4",
        "status": "imported",
    }
    defaults.update(overrides)
    row = WatchSourceFile(**defaults)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def _no_scan():
    """Patch the scan dispatch — assert on the call, never queue real work."""
    with patch("app.tasks.watch_source_tasks.scan_single.delay") as delay:
        yield delay


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_returns_the_diagnostic_fields_a_row_carries(
    client, db_session, normal_user, user_token_headers
):
    """``skip_reason``/``error_message``/``retry_count`` must survive serialization.

    They are the entire reason the file table exists — an admin looking at a failed
    import needs to know *why*, and the handler builds its response dict by hand, so
    a field can be dropped without any schema change to notice it.
    """
    source = _make_source(db_session, normal_user)
    _make_row(
        db_session,
        source,
        filename="broken.mp4",
        status="error",
        error_message="download produced no bytes",
        retry_count=2,
    )
    _make_row(db_session, source, filename="old.mp4", status="skipped_old", skip_reason="too_old")

    response = client.get(f"{BASE}/{source.uuid}/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    by_name = {f["filename"]: f for f in body["files"]}
    assert by_name["broken.mp4"]["error_message"] == "download produced no bytes"
    assert by_name["broken.mp4"]["retry_count"] == 2
    assert by_name["old.mp4"]["skip_reason"] == "too_old"


def test_status_filter_returns_only_matching_rows(
    client, db_session, normal_user, user_token_headers
):
    """Asserts the filter EXCLUDES, not merely that it returns few enough rows.

    ``len(files) <= total`` is the shape of assertion that passes against an empty
    table and proves nothing; the imported row below is what makes this falsifiable.
    """
    source = _make_source(db_session, normal_user)
    _make_row(db_session, source, filename="failed.mp4", status="error")
    _make_row(db_session, source, filename="fine.mp4", status="imported")

    response = client.get(
        f"{BASE}/{source.uuid}/files", params={"status": "error"}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert [f["filename"] for f in response.json()["files"]] == ["failed.mp4"]


def test_query_filter_matches_a_filename_substring(
    client, db_session, normal_user, user_token_headers
):
    """A source can track thousands of files; paging to find one is not a workflow."""
    source = _make_source(db_session, normal_user)
    _make_row(db_session, source, filename="2026-board-meeting.mp4")
    _make_row(db_session, source, filename="standup.mp4")

    response = client.get(
        f"{BASE}/{source.uuid}/files", params={"q": "BOARD"}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert [f["filename"] for f in response.json()["files"]] == ["2026-board-meeting.mp4"]


def test_query_filter_treats_wildcards_literally(
    client, db_session, normal_user, user_token_headers
):
    """``%`` is a legal filename character and must not become a match-everything.

    Unescaped, searching for ``%`` returns the whole table — which reads as "search is
    broken" but is worse: it silently contradicts the filter the user asked for.
    """
    source = _make_source(db_session, normal_user)
    _make_row(db_session, source, filename="100%-final.mp4")
    _make_row(db_session, source, filename="draft.mp4")

    response = client.get(
        f"{BASE}/{source.uuid}/files", params={"q": "%"}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert [f["filename"] for f in response.json()["files"]] == ["100%-final.mp4"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_removes_the_tracking_row(client, db_session, normal_user, user_token_headers):
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source)

    response = client.delete(f"{BASE}/{source.uuid}/files/{row.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert db_session.query(WatchSourceFile).filter(WatchSourceFile.uuid == row.uuid).count() == 0


def test_delete_unknown_row_is_404(client, db_session, normal_user, user_token_headers):
    source = _make_source(db_session, normal_user)

    response = client.delete(
        f"{BASE}/{source.uuid}/files/{ABSENT_UUID}", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_delete_on_another_users_source_is_403(
    client, db_session, normal_user, other_user_auth_headers
):
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source)

    response = client.delete(
        f"{BASE}/{source.uuid}/files/{row.uuid}", headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_bulk_delete_reports_per_row_and_keeps_the_valid_ones(
    client, db_session, normal_user, user_token_headers
):
    """A stale uuid in the batch must not discard the work for the valid rows.

    The caller is acting on a page of results that a concurrent scan may have changed
    underneath them, so one miss is that row's problem, not the request's.
    """
    source = _make_source(db_session, normal_user)
    first = _make_row(db_session, source)
    second = _make_row(db_session, source)

    response = client.post(
        f"{BASE}/{source.uuid}/files/bulk-delete",
        json={"file_uuids": [str(first.uuid), ABSENT_UUID, str(second.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK
    results = {r["file_uuid"]: r for r in response.json()["results"]}
    assert results[str(first.uuid)]["success"] is True
    assert results[str(second.uuid)]["success"] is True
    assert results[ABSENT_UUID]["success"] is False
    remaining = (
        db_session.query(WatchSourceFile)
        .filter(WatchSourceFile.watch_source_id == source.id)
        .count()
    )
    assert remaining == 0


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "starting_status",
    ["error", "skipped_duplicate", "skipped_old", "skipped_invalid"],
)
def test_retry_requeues_a_retryable_row(
    client, db_session, normal_user, user_token_headers, _no_scan, starting_status
):
    """Every retryable status resets to ``pending`` and clears its reason.

    The ``skipped_*`` cases are the new capability: those are terminal, and
    ``_get_or_create_tracking_row`` refuses to reuse a terminal row, so nothing could
    re-import them before. Clearing ``skip_reason``/``error_message`` matters as much
    as the status — a stale reason left on a re-queued row is what the table shows the
    operator while it waits.
    """
    source = _make_source(db_session, normal_user)
    row = _make_row(
        db_session,
        source,
        status=starting_status,
        skip_reason="duplicate_existing",
        error_message="something went wrong",
    )

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(row.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["results"][0]["success"] is True
    db_session.expire_all()
    refreshed = db_session.get(WatchSourceFile, row.id)
    assert refreshed.status == "pending"
    assert refreshed.skip_reason is None
    assert refreshed.error_message is None


@pytest.mark.parametrize(
    "blocked_status",
    ["imported", "importing", "downloading", "waiting_for_parts", "stitched_part"],
)
def test_retry_refuses_a_state_it_must_not_touch(
    client, db_session, normal_user, user_token_headers, _no_scan, blocked_status
):
    """Each refusal prevents a different concrete harm.

    ``imported`` would duplicate content; ``importing``/``downloading`` race
    ``_claim_import``; ``waiting_for_parts`` carries the multipart WAIT counter in
    ``retry_count`` rather than an attempt count, so resetting it corrupts the stitch
    decision; ``stitched_part`` was consumed into a stitched recording and would come
    back alongside the whole.
    """
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source, status=blocked_status)

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(row.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["results"][0]["success"] is False
    assert body["scan_dispatched"] is False
    db_session.expire_all()
    assert db_session.get(WatchSourceFile, row.id).status == blocked_status
    _no_scan.assert_not_called()


def test_retry_dispatches_exactly_one_scan_for_a_whole_batch(
    client, db_session, normal_user, user_token_headers, _no_scan
):
    """The reason the endpoint is batch-shaped at all.

    ``scan_single`` takes a Redis lock per source, so N per-file dispatches would run
    one scan and silently discard N-1. Anything that reintroduces a per-row dispatch
    fails here.
    """
    source = _make_source(db_session, normal_user)
    rows = [_make_row(db_session, source, status="error") for _ in range(3)]

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(r.uuid) for r in rows]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["scan_dispatched"] is True
    _no_scan.assert_called_once_with(source.id)


def test_retry_does_not_inflate_the_attempt_count(
    client, db_session, normal_user, user_token_headers, _no_scan
):
    """``_record_error`` already counts each failure; counting here too double-counts.

    The number is shown to the operator, so an inflated one misreports how hard the
    system has already tried.
    """
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source, status="error", retry_count=2)

    client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(row.uuid)]},
        headers=user_token_headers,
    )

    db_session.expire_all()
    assert db_session.get(WatchSourceFile, row.id).retry_count == 2


def test_retry_warns_when_the_age_limit_will_just_skip_it_again(
    client, db_session, normal_user, user_token_headers, _no_scan
):
    """A retry that cannot succeed must say so rather than look like progress.

    The row resets and the scan is dispatched — but ``_record_age_skips`` re-skips it
    immediately, so without the warning the operator watches it flip back and has no
    way to know the age setting is what did it.
    """
    source = _make_source(db_session, normal_user, skip_files_older_than_days=30)
    row = _make_row(db_session, source, status="skipped_old", skip_reason="too_old")

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(row.uuid)]},
        headers=user_token_headers,
    )

    result = response.json()["results"][0]
    assert result["success"] is True
    assert result["warning"] and "30 days" in result["warning"]


def test_retry_on_a_disabled_source_is_409_and_changes_nothing(
    client, db_session, normal_user, user_token_headers, _no_scan
):
    """``_load_scan_plan`` returns None for a disabled source, so the scan is a no-op.

    Resetting rows for a scan that will not run would report success for work that can
    never happen — and would leave terminal rows sitting in ``pending`` indefinitely.
    """
    source = _make_source(db_session, normal_user, is_enabled=False)
    row = _make_row(db_session, source, status="error")

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(row.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    db_session.expire_all()
    assert db_session.get(WatchSourceFile, row.id).status == "error"
    _no_scan.assert_not_called()


def test_retry_on_another_users_source_is_403(
    client, db_session, normal_user, other_user_auth_headers, _no_scan
):
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source, status="error")

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry",
        json={"file_uuids": [str(row.uuid)]},
        headers=other_user_auth_headers,
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_retry_requires_authentication(client, db_session, normal_user):
    source = _make_source(db_session, normal_user)

    response = client.post(f"{BASE}/{source.uuid}/files/retry", json={"file_uuids": [ABSENT_UUID]})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_retry_rejects_an_empty_batch(client, db_session, normal_user, user_token_headers):
    """An empty list is a caller mistake, not a no-op success.

    Answering 200 would tell the UI a scan was queued when nothing was even asked for.
    """
    source = _make_source(db_session, normal_user)

    response = client.post(
        f"{BASE}/{source.uuid}/files/retry", json={"file_uuids": []}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
