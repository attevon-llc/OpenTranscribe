"""The scale seed's load-test rows: comments, per-user tags and cached waveforms.

Counts, the properties the load test relies on (a comment is written by the file's owner, a
personal tag is its owner's and sits on the owner's own file, a cached waveform has the shape
the waveform route accepts) and determinism.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.scripts.scale_dataset import pg_synthetic as pg
from app.scripts.scale_dataset.pg_activity import WAVEFORM_SAMPLES
from app.scripts.scale_dataset.pg_governance import seed_all

# Seeds write fixed natural keys (perf-N@loadtest.invalid) into uncommitted rows, so two of
# these open on different workers block on each other's unique index while each still holds the
# shared DDL lock, which stalls every ddl_exclusive test queued behind them. One group, one worker.
pytestmark = pytest.mark.xdist_group("scale_dataset_seed")

SHAPE = pg.Shape(
    users=6,
    files=20,
    heavy_user_files=8,
    speakers_per_file=2,
    segments_per_file=2,
    tasks_per_file=1,
    profiles=4,
    tags=6,
    tags_per_file=3,
    orgs=2,
    holds=2,
    hold_files=4,
    collections=3,
    collection_members=6,
    collection_shares=2,
    clusters=2,
    cluster_members=4,
    matches=10,
    hidden=4,
    conversations=2,
    messages=4,
    comments=30,
    user_tags=10,
    waveform_pm=500,
)
_SEEDED = "storage_path LIKE 'loadtest/{seed}/%'"


def _seed(db_session, seed: str = "a1337") -> dict[str, int]:
    return seed_all(db_session.connection(), SHAPE, seed, "x-not-a-real-hash")


def _scalar(db_session, sql: str, seed: str = "a1337") -> object:
    return (
        db_session.connection()
        .execute(text(sql.format(seeded=_SEEDED.format(seed=seed))))
        .scalar_one()
    )


def test_the_activity_rows_have_the_requested_counts(db_session):
    counts = _seed(db_session)

    assert counts["comment"] == 30
    assert counts["user_tag"] == 10
    assert counts["user_file_tag"] == 10
    assert counts["waveform"] == 10  # 500 per mille of 20 files


def test_each_comment_is_written_by_the_owner_of_its_file(db_session):
    _seed(db_session)

    foreign = _scalar(
        db_session,
        "SELECT count(*) FROM comment c JOIN media_file m ON m.id = c.media_file_id "
        "WHERE m.{seeded} AND c.user_id <> m.user_id",
    )
    total = _scalar(
        db_session,
        "SELECT count(*) FROM comment c JOIN media_file m ON m.id = c.media_file_id WHERE m.{seeded}",
    )

    assert total == 30
    assert foreign == 0


def test_a_personal_tag_belongs_to_an_ordinary_owner_and_tags_their_own_file(db_session):
    _seed(db_session)

    rows = (
        db_session.connection()
        .execute(
            text(
                "SELECT t.user_id = m.user_id, u.email FROM tag t "
                "JOIN file_tag ft ON ft.tag_id = t.id JOIN media_file m ON m.id = ft.media_file_id "
                'JOIN "user" u ON u.id = t.user_id '
                "WHERE t.name LIKE 'loadtest-mytag-%' AND m.storage_path LIKE 'loadtest/a1337/%'"
            )
        )
        .all()
    )

    assert len(rows) == 10
    assert all(same for same, _ in rows)
    # Never the admin (perf-1) or the heavy owner (perf-2): those already own the shared tags.
    assert not {email for _, email in rows} & {"perf-1@loadtest.invalid", "perf-2@loadtest.invalid"}


def test_a_cached_waveform_has_the_shape_the_waveform_route_accepts(db_session):
    _seed(db_session)
    key = f"waveform_{WAVEFORM_SAMPLES}"

    entry = (
        db_session.connection()
        .execute(
            text(
                "SELECT waveform_data -> :key FROM media_file "
                "WHERE storage_path LIKE 'loadtest/a1337/%' AND waveform_data IS NOT NULL LIMIT 1"
            ),
            {"key": key},
        )
        .scalar_one()
    )

    # api/endpoints/files/waveform.py::_get_cached_waveform rejects an entry without these.
    assert {"waveform", "extracted_samples", "expected_duration"} <= set(entry)
    assert len(entry["waveform"]) == WAVEFORM_SAMPLES


def test_two_seeds_generate_identical_activity_rows(db_session):
    def fingerprint() -> tuple:
        return tuple(
            _scalar(db_session, sql)
            for sql in (
                "SELECT md5(string_agg(c.text || '|' || c.timestamp || '|' || m.uuid, ',' "
                "ORDER BY c.text)) FROM comment c JOIN media_file m ON m.id = c.media_file_id "
                "WHERE m.{seeded}",
                "SELECT md5(string_agg(t.name || '|' || m.uuid, ',' ORDER BY t.name)) FROM tag t "
                "JOIN file_tag ft ON ft.tag_id = t.id JOIN media_file m ON m.id = ft.media_file_id "
                "WHERE t.name LIKE 'loadtest-mytag-%' AND m.{seeded}",
                "SELECT md5(string_agg(uuid::text, ',' ORDER BY uuid)) FROM media_file "
                "WHERE {seeded} AND waveform_data IS NOT NULL",
            )
        )

    _seed(db_session)
    first = fingerprint()
    db_session.rollback()
    _seed(db_session)
    second = fingerprint()

    assert first == second
    assert None not in first


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"user_tags": 13}, "user_tags"),  # only 12 non-heavy files to put them on
        ({"comments": -1}, "comments"),
        ({"waveform_pm": 1001}, "waveform_pm"),
    ],
)
def test_an_impossible_activity_shape_is_refused(override, message):
    fields = {**SHAPE.__dict__, **override}

    with pytest.raises(ValueError, match=message):
        pg.Shape(**fields)
