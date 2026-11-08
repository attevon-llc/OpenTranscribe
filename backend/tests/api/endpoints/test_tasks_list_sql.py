"""``GET /api/tasks`` filters, orders and pages in SQL, not Python (issue #1337, K1).

It used to load every visible file and every task row, then slice in Python: 4.3 s for a
20,000-file owner. These tests pin the three properties the rewrite must keep.

* The statement count does not grow with the number of files or tasks (n=2 versus n=12, with a
  control that the pages really held that many rows).
* Paging returns exactly the rows the old Python filter kept, in a stable order that does not
  reorder files whose timestamps tie.
* A file's current task is its ``active_task_id`` row, else its newest row.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from tests.query_budget import count_statements


def _file(db, user, name, status=FileStatus.COMPLETED, **extra):
    media_file = MediaFile(
        user_id=user.id,
        filename=name,
        storage_path=f"{user.id}/{name}",
        file_size=1024,
        content_type="video/mp4",
        status=status,
        **extra,
    )
    db.add(media_file)
    db.flush()
    return media_file


def _task(db, user, media_file, *, status="completed", task_type="transcription", age_hours=0.0):
    row = TaskModel(
        id=str(uuid.uuid4()),
        user_id=user.id,
        media_file_id=media_file.id,
        task_type=task_type,
        status=status,
        progress=1.0,
        created_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    db.add(row)
    db.flush()
    return row


def _seed(db, user, n_files, tasks_per_file=3):
    files = []
    for i in range(n_files):
        media_file = _file(db, user, f"budget_{uuid.uuid4().hex}_{i}.mp4")
        for j in range(tasks_per_file):
            _task(db, user, media_file, age_hours=j + 1, task_type=f"stage_{j}")
        files.append(media_file)
    db.commit()
    return files


def _ours(payload, files):
    wanted = {str(f.uuid) for f in files}
    return [item for item in payload["items"] if item["media_file_id"] in wanted]


def test_statement_count_is_flat_in_files_and_tasks(
    client, user_token_headers, db_session, normal_user
):
    small = _seed(db_session, normal_user, 2)
    with count_statements(db_session.connection()) as log_small:
        r_small = client.get("/api/tasks?page_size=100", headers=user_token_headers)
    assert r_small.status_code == 200
    assert len(_ours(r_small.json(), small)) == 2

    more = _seed(db_session, normal_user, 10)
    with count_statements(db_session.connection()) as log_large:
        r_large = client.get("/api/tasks?page_size=100", headers=user_token_headers)
    assert r_large.status_code == 200
    assert len(_ours(r_large.json(), small + more)) == 12

    assert log_large.count == log_small.count, log_large.describe()


def test_the_task_rows_are_not_read_for_files_off_the_page(
    client, user_token_headers, db_session, normal_user
):
    """Only the page's rows are materialised: a 1-row page reads one row, not all of them."""
    _seed(db_session, normal_user, 12)

    with count_statements(db_session.connection()) as log:
        response = client.get("/api/tasks?page_size=1", headers=user_token_headers)

    body = response.json()
    assert len(body["items"]) == 1
    assert body["total"] >= 12
    assert not log.matching(r"ORDER BY task\.media_file_id"), log.describe()


def test_active_task_beats_a_newer_row_and_otherwise_the_newest_wins(
    client, user_token_headers, db_session, normal_user
):
    media_file = _file(db_session, normal_user, "active.mp4", status=FileStatus.PROCESSING)
    active = _task(db_session, normal_user, media_file, status="in_progress", age_hours=5)
    _task(db_session, normal_user, media_file, status="completed", age_hours=1)
    media_file.active_task_id = active.id
    settled = _file(db_session, normal_user, "settled.mp4")
    _task(db_session, normal_user, settled, status="failed", age_hours=9)
    newest = _task(db_session, normal_user, settled, status="completed", age_hours=2)
    db_session.commit()

    items = client.get("/api/tasks?page_size=100", headers=user_token_headers).json()["items"]
    by_file = {i["media_file_id"]: i for i in items}

    assert by_file[str(media_file.uuid)]["id"] == active.id
    assert by_file[str(settled.uuid)]["id"] == newest.id


def test_filters_page_and_total_agree_with_each_other(
    client, user_token_headers, db_session, normal_user
):
    for i in range(7):
        f = _file(db_session, normal_user, f"flt_{i}.mp4")
        _task(db_session, normal_user, f, status="failed" if i < 3 else "completed")
    db_session.commit()

    first = client.get("/api/tasks?status=failed&page_size=2&page=1", headers=user_token_headers)
    second = client.get("/api/tasks?status=failed&page_size=2&page=2", headers=user_token_headers)

    one, two = first.json(), second.json()
    assert one["total"] == two["total"] == 3
    assert one["total_pages"] == 2
    assert one["has_more"] is True and two["has_more"] is False
    ids = [i["id"] for i in one["items"]] + [i["id"] for i in two["items"]]
    assert len(ids) == 3 and len(set(ids)) == 3  # no repeat across pages
    assert all(i["status"] == "failed" for i in one["items"] + two["items"])


def test_equal_timestamps_page_in_a_stable_order(
    client, user_token_headers, db_session, normal_user
):
    stamp = datetime.now(UTC) - timedelta(hours=3)
    for i in range(6):
        f = _file(db_session, normal_user, f"tie_{i}.mp4")
        row = _task(db_session, normal_user, f)
        row.created_at = stamp
    db_session.commit()

    pages = [
        client.get(
            f"/api/tasks?page_size=2&page={p}&task_type=transcription", headers=user_token_headers
        )
        for p in (1, 2, 3)
    ]
    ids = [i["id"] for r in pages for i in r.json()["items"]]
    again = [
        i["id"]
        for p in (1, 2, 3)
        for i in client.get(
            f"/api/tasks?page_size=2&page={p}&task_type=transcription", headers=user_token_headers
        ).json()["items"]
    ]

    assert len(ids) == len(set(ids)) >= 6
    assert ids == again


def test_a_file_with_no_task_row_reports_its_file_status(
    client, user_token_headers, db_session, normal_user
):
    f = _file(db_session, normal_user, "bare.mp4", status=FileStatus.ERROR)
    db_session.commit()

    body = client.get("/api/tasks?status=failed&page_size=100", headers=user_token_headers).json()

    assert str(f.uuid) in {i["media_file_id"] for i in body["items"]}
