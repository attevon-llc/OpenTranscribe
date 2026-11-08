"""``/admin/stats`` and ``/system/stats`` run off the event loop and use few statements (#1337).

A1: ``get_admin_stats`` was ``async def`` and ran synchronous database calls on the event loop;
FastAPI runs a plain ``def`` handler in its threadpool
(https://fastapi.tiangolo.com/async/#in-a-hurry). Both endpoints also did two exact
``count(*)`` scans over multi-million-row tables on every poll.
A2: ``/system/stats`` issued 15 statements; the same-table ones are now one each.
"""

from __future__ import annotations

import inspect

from app.api.endpoints import admin
from app.models.media import MediaFile
from tests.query_budget import count_statements


def test_admin_stats_handler_is_not_a_coroutine():
    assert not inspect.iscoroutinefunction(admin.get_admin_stats)


def test_system_stats_reads_each_hot_table_once(client, admin_token_headers, db_session):
    with count_statements(db_session.connection()) as log:
        response = client.get("/api/system/stats", headers=admin_token_headers)

    assert response.status_code == 200, response.text
    assert len(log.matching(r"FROM task\b")) == 2, log.describe()  # aggregate + recent list
    assert len(log.matching(r"FROM media_file\b")) == 2, log.describe()  # file stats + flow
    body = response.json()
    assert body["transcripts"]["total_segments_is_estimate"] in (True, False)
    assert body["speakers"]["total_is_estimate"] in (True, False)
    assert "throughput" in body and "eta" in body and "file_timing" in body


def test_admin_stats_statement_count_is_flat_in_media_rows(
    client, admin_token_headers, db_session, admin_user
):
    def add(n):
        for i in range(n):
            db_session.add(
                MediaFile(
                    user_id=admin_user.id,
                    filename=f"budget_{i}_{id(db_session)}_{n}.wav",
                    storage_path=f"budget/{n}/{i}_{id(db_session)}.wav",
                    file_size=10,
                    content_type="audio/wav",
                    status="completed",
                )
            )
        db_session.commit()

    add(2)
    with count_statements(db_session.connection()) as small:
        r_small = client.get("/api/admin/stats", headers=admin_token_headers)
    total_small = r_small.json()["files"]["total"]
    add(10)
    with count_statements(db_session.connection()) as large:
        r_large = client.get("/api/admin/stats", headers=admin_token_headers)

    assert r_small.status_code == r_large.status_code == 200
    assert r_large.json()["files"]["total"] >= total_small + 10  # control: rows were counted
    assert large.count == small.count, large.describe()
