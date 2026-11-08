"""The embedding-consistency beat task reads speakers in bounded chunks (issue #1337, V10).

It ran every 10 minutes and loaded every segment of every missing speaker through one
``IN (<8.9 MB list>)`` statement (10M rows on a large library), and read the whole speaker
table in single statements. Now: keyset pages / fixed-size chunks, each in its own short
session, so memory is bounded and no transaction spans chunks.
"""

import contextlib
import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.tasks import speaker_embedding_consistency as mod
from tests.query_budget import count_statements


@pytest.fixture
def scopes(db_session, monkeypatch):
    """Share the test session; record how many scopes opened and whether any nested."""
    state = {"opened": 0, "depth": 0, "max_depth": 0}

    @contextlib.contextmanager
    def scope():
        state["opened"] += 1
        state["depth"] += 1
        state["max_depth"] = max(state["max_depth"], state["depth"])
        try:
            yield db_session
        finally:
            state["depth"] -= 1

    monkeypatch.setattr(mod, "session_scope", scope)
    return state


def _speakers(db_session, owner, n: int, *, seg_len: float | None) -> list[Speaker]:
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="chunked-scan.mp4",
        storage_path=f"chunked-scan/{uuid_pkg.uuid4().hex}.mp4",
        content_type="video/mp4",
        file_size=1,
        user_id=owner.id,
    )
    db_session.add(media)
    db_session.flush()
    rows = [
        Speaker(user_id=owner.id, media_file_id=media.id, name=f"SPEAKER_{i:02d}") for i in range(n)
    ]
    db_session.add_all(rows)
    db_session.flush()
    if seg_len is not None:
        db_session.add_all(
            TranscriptSegment(
                media_file_id=media.id,
                speaker_id=s.id,
                start_time=float(i * 100),
                end_time=i * 100 + seg_len,
                text="x",
            )
            for i, s in enumerate(rows)
        )
    db_session.flush()
    return rows


def test_unrepairable_filter_loads_segments_in_bounded_chunks(
    db_session, normal_user, scopes, monkeypatch
):
    monkeypatch.setattr(mod, "_UNREPAIRABLE_CHUNK_SIZE", 3)
    short = _speakers(db_session, normal_user, 4, seg_len=0.1)  # too short: unrepairable
    long_ = _speakers(db_session, normal_user, 5, seg_len=5.0)  # repairable
    missing = {str(s.uuid) for s in short + long_}

    with count_statements(db_session.connection()) as log:
        result = mod._filter_unrepairable_speakers(missing)

    assert result == {str(s.uuid) for s in short}  # control: real classification
    segment_selects = log.matching(r"FROM transcript_segment")
    assert len(segment_selects) == 3  # ceil(9 / 3) chunks, never one statement for all
    assert scopes["opened"] == 3 and scopes["max_depth"] == 1, scopes


def test_speaker_scans_page_by_keyset(db_session, normal_user, scopes, monkeypatch):
    monkeypatch.setattr(mod, "_SPEAKER_PAGE_SIZE", 4)
    mine = _speakers(db_session, normal_user, 6, seg_len=1.0)
    bare = _speakers(db_session, normal_user, 3, seg_len=None)

    with_segments = mod._get_pg_speaker_uuids_with_segments()
    everyone = mod._get_all_pg_speaker_uuids()

    assert {str(s.uuid) for s in mine} <= set(with_segments)
    assert not {str(s.uuid) for s in bare} & set(with_segments)
    assert {str(s.uuid) for s in mine + bare} <= everyone
    assert scopes["max_depth"] == 1  # one session per page, never nested or spanning pages
    assert scopes["opened"] >= 2 * (len(everyone) // 4)  # paged, not one statement
