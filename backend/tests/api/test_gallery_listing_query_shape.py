"""Gallery paging: stable order, no second count for a short page, flat statements (#1337).

G3: rows with equal sort keys need an ``id`` tie-breaker or paging repeats and skips rows.
G2: a short page already tells the total, so the count (every filter and scope run twice) is
skipped. G10: the privacy view reads the page's file scopes in one statement.
Each claim has a control: the full-page path still counts, and the responses really hold
both sizes' rows.
"""

from __future__ import annotations

import pytest

from app.tasks.transcription.speaker_processor import create_or_get_speaker
from tests.query_budget import count_statements

FILES = "/api/files"


def _page(client, gov, **params):
    with gov.acting_as(gov.alice):
        response = client.get(FILES, params={"ownership": "mine", **params})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("sort_by", ["file_size", "duration", "completed_at", "filename"])
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_paging_rows_with_equal_sort_keys_neither_repeats_nor_skips(
    client, gov, sort_by, sort_order
):
    files = [gov.file(gov.alice, filename="same.mp3", duration=60.0) for _ in range(13)]
    expected = {str(f.uuid) for f in files}

    seen: list[str] = []
    for page in (1, 2, 3, 4):
        body = _page(client, gov, page=page, page_size=4, sort_by=sort_by, sort_order=sort_order)
        seen.extend(item["uuid"] for item in body["items"])

    assert len(seen) == len(set(seen)), "a row was served on two pages"
    assert set(seen) == expected, "a row was served on no page"
    assert body["total"] == 13


def test_every_sort_orders_by_id_last(client, gov):
    gov.file(gov.alice)
    with count_statements(gov.db.connection()) as log:
        for sort_by in ("upload_time", "completed_at", "filename", "duration", "file_size"):
            _page(client, gov, sort_by=sort_by)
    pages = log.matching(r"FROM media_file.*ORDER BY.*LIMIT")
    assert len(pages) >= 5, log.describe()
    assert all(
        s.split("ORDER BY")[-1]
        .split("LIMIT")[0]
        .strip()
        .split(",")[-1]
        .strip()
        .startswith("media_file.id")
        for s in pages
    ), log.describe()


def test_a_short_page_skips_the_count_and_a_full_page_still_counts(client, gov):
    for _ in range(3):
        gov.file(gov.alice)

    with count_statements(gov.db.connection()) as short_log:
        short = _page(client, gov, page_size=10)
    with count_statements(gov.db.connection()) as full_log:
        full = _page(client, gov, page_size=3)

    assert (short["total"], len(short["items"])) == (3, 3)
    assert short_log.matching(r"count\(media_file\.id\)") == [], short_log.describe()
    assert (full["total"], len(full["items"])) == (3, 3)
    assert full_log.matching(r"count\(media_file\.id\)"), "control: a full page must count"


def test_an_empty_later_page_reports_the_real_total(client, gov):
    for _ in range(3):
        gov.file(gov.alice)

    body = _page(client, gov, page=3, page_size=2)

    assert body["items"] == []
    assert body["total"] == 3
    assert body["has_more"] is False


def test_statements_per_page_do_not_grow_with_the_files_on_it(client, gov):
    def _seed(n: int) -> None:
        for _ in range(n):
            media = gov.file(gov.alice)
            for label in ("SPEAKER_00", "SPEAKER_01"):
                create_or_get_speaker(gov.db, gov.alice.id, media.id, label)
        gov.db.commit()

    _seed(2)
    with count_statements(gov.db.connection()) as small:
        small_body = _page(client, gov, page_size=50)
    _seed(10)
    with count_statements(gov.db.connection()) as large:
        large_body = _page(client, gov, page_size=50)

    assert (len(small_body["items"]), len(large_body["items"])) == (2, 12)
    assert large.count == small.count, f"{small.describe()}\n---\n{large.describe()}"
