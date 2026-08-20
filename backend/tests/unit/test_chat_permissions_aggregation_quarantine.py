"""Quarantine enforcement on the chat aggregation ("counted") tier (W2.0g fix #1,
the finding the earlier pass shipped incomplete-but-documented-complete).

`_drop_quarantined_hits` (``service.py`` phase 3.5) only ever filtered the
RANKED tiers — `result.chunks` and `result.digests`. The counted tier
(`aggregation_service.answer_aggregation`) runs a phase EARLIER and was never
routed through it, so a non-admin with no admin bypass involved could still
learn a taken-down file's existence, title, speaker roster or occurrence count
by asking an aggregate question instead of an ordinary one:

* ``_occurrence_count`` (SHAPE_COUNT_EVENTS, Postgres) counted the quarantined
  file's segments.
* ``_files_matching`` (SHAPE_COUNT_FILES / SHAPE_LIST_FILES, OpenSearch)
  returned the quarantined file's uuid AND TITLE, which
  ``prompting.format_counted_block`` renders into the ``<counted>`` block —
  and base rule 10 tells the model to report it exactly.
* ``_speaker_tally`` / ``_run_speaker_facet`` (SHAPE_SPEAKER_FACET, OpenSearch)
  counted the quarantined file's session toward its speakers' tallies.

Each shape gets a LEAK case (a quarantined file contributes exactly zero) and
a SHARED-VISIBILITY control (an ordinary accessible file is untouched) — a fix
that also drops legitimate content is a failure, not a pass, per this repo's
standing rule for permission fixes.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.services.chat.aggregation_service import _occurrence_count
from app.services.chat.aggregation_service import answer_aggregation
from app.services.chat.router import route
from tests.unit.test_chat_aggregation import _file_agg
from tests.unit.test_chat_aggregation import _people_agg
from tests.unit.test_chat_aggregation import _RecordingClient

pytestmark = pytest.mark.unit


def _make_file(db, user, *, quarantined=False, title="Recording"):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _add_segment(db, media_file, text):
    from app.models.media import TranscriptSegment

    segment = TranscriptSegment(
        media_file_id=media_file.id, start_time=0.0, end_time=5.0, text=text
    )
    db.add(segment)
    db.commit()
    return segment


@contextmanager
def _factory(db_session):
    """A session_factory over the test's own savepoint-isolated session — the
    real production shape is ``db.session_utils.session_scope``, opened per
    statement group; here every group shares the one test session so writes
    made by the fixture are visible."""
    yield db_session


def _sf(db_session):
    def _open():
        return _factory(db_session)

    return _open


def _file_agg_with_titles(*pairs: tuple[str, str]) -> dict:
    """Like ``test_chat_aggregation._file_agg`` but with the nested ``title``
    sub-bucket real ``_files_matching`` responses carry — the bare helper omits
    it, which makes every title read back as ``""`` and hides a title leak."""
    return {
        "aggregations": {
            "files": {
                "sum_other_doc_count": 0,
                "buckets": [
                    {
                        "key": uuid,
                        "doc_count": 1,
                        "title": {"buckets": [{"key": title}]},
                    }
                    for uuid, title in pairs
                ],
            }
        }
    }


# ---------------------------------------------------------------------------
# `_occurrence_count` — SHAPE_COUNT_EVENTS, Postgres
# ---------------------------------------------------------------------------


def test_occurrence_count_excludes_a_quarantined_files_segments(db_session, normal_user):
    """LEAK: a taken-down file's occurrences of the subject must not count."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")
    _add_segment(db_session, blocked, "gizmo gizmo gizmo")

    count = _occurrence_count(db_session, "gizmo", normal_user.id, None, None)

    assert count == 0


def test_occurrence_count_still_counts_an_ordinary_accessible_file(db_session, normal_user):
    """SHARED-VISIBILITY control: an accessible, non-quarantined file still counts."""
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")
    _add_segment(db_session, ok, "gizmo gizmo")

    count = _occurrence_count(db_session, "gizmo", normal_user.id, None, None)

    assert count == 2


def test_occurrence_count_excludes_only_the_quarantined_file_in_a_mixed_set(
    db_session, normal_user
):
    """One quarantined file must not take the rest of the count down with it."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")
    _add_segment(db_session, blocked, "gizmo")
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")
    _add_segment(db_session, ok, "gizmo gizmo")

    count = _occurrence_count(db_session, "gizmo", normal_user.id, None, None)

    assert count == 2


# ---------------------------------------------------------------------------
# `_files_matching` (via `answer_aggregation`) — SHAPE_COUNT_FILES / LIST_FILES
# ---------------------------------------------------------------------------


def test_list_files_shape_drops_a_quarantined_files_uuid_and_title(db_session, normal_user):
    """LEAK: `<counted>` must never carry a taken-down file's uuid or title —
    base rule 10 tells the model to report the block exactly."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Secret Merger Call")

    question = "Which meetings mention the Atlas migration? List them."
    client = _RecordingClient(_file_agg_with_titles((str(blocked.uuid), "Secret Merger Call")))

    result = answer_aggregation(
        question,
        route(question),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert result.count == 0
    assert result.file_uuids == ()
    assert result.file_titles == ()


def test_list_files_shape_still_returns_an_ordinary_accessible_file(db_session, normal_user):
    """SHARED-VISIBILITY control: an accessible, non-quarantined file is untouched."""
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine Meeting")

    question = "Which meetings mention the Atlas migration? List them."
    client = _RecordingClient(_file_agg_with_titles((str(ok.uuid), "Fine Meeting")))

    result = answer_aggregation(
        question,
        route(question),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert result.count == 1
    assert result.file_uuids == (str(ok.uuid),)
    assert result.file_titles == ("Fine Meeting",)


def test_list_files_shape_drops_only_the_quarantined_entry_from_a_mixed_result(
    db_session, normal_user
):
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")

    question = "Which meetings mention the Atlas migration? List them."
    client = _RecordingClient(_file_agg(str(blocked.uuid), str(ok.uuid)))

    result = answer_aggregation(
        question,
        route(question),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert result.file_uuids == (str(ok.uuid),)


# ---------------------------------------------------------------------------
# `_speaker_tally` (via `answer_aggregation`) — SHAPE_SPEAKER_FACET
# ---------------------------------------------------------------------------


def test_speaker_facet_excludes_a_quarantined_files_session_from_the_tally(db_session, normal_user):
    """LEAK: a taken-down recording's session must not count toward any
    speaker's attendance tally."""
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")
    # Bo's two ordinary sessions — real (non-existent-in-DB) uuids, since
    # `_quarantined_among` runs a real Postgres `.in_(...)` query against the
    # `MediaFile.uuid` column, which is UUID-typed: a non-uuid placeholder
    # string like "f1" would raise `invalid input syntax for type uuid`
    # rather than simply not match.
    bo_file_1, bo_file_2 = uuid_pkg.uuid4(), uuid_pkg.uuid4()

    question = "Who attended the most design review sessions?"
    # Ada's only session is the quarantined recording; Bo has two ordinary ones.
    people_agg = {
        "aggregations": {
            "people": {
                "sum_other_doc_count": 0,
                "buckets": [
                    {
                        "key": "Ada",
                        "files": {
                            "sum_other_doc_count": 0,
                            "buckets": [{"key": str(blocked.uuid)}],
                        },
                    },
                    {
                        "key": "Bo",
                        "files": {
                            "sum_other_doc_count": 0,
                            "buckets": [{"key": str(bo_file_1)}, {"key": str(bo_file_2)}],
                        },
                    },
                ],
            }
        }
    }
    client = _RecordingClient(people_agg)

    result = answer_aggregation(
        question,
        route(question),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    # Ada's only session was quarantined and dropped to zero, so she must not
    # appear at all — a zero-session "attendee" is not an attendee.
    assert "Ada" not in dict(result.rows)
    assert (result.speaker, result.speaker_sessions) == ("Bo", 2)


def test_speaker_facet_still_tallies_an_ordinary_accessible_files_session(db_session, normal_user):
    """SHARED-VISIBILITY control: an ordinary file's session still counts."""
    ok = _make_file(db_session, normal_user, quarantined=False, title="Fine")

    question = "Who attended the most design review sessions?"
    client = _RecordingClient(_people_agg(("Ada", 1)))
    # Point Ada's one session at the real, non-quarantined file uuid.
    client.response["aggregations"]["people"]["buckets"][0]["files"]["buckets"] = [
        {"key": str(ok.uuid)}
    ]

    result = answer_aggregation(
        question,
        route(question),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert (result.speaker, result.speaker_sessions) == ("Ada", 1)
