"""W2.4 fix #1: the speaker facet is scored by TITLE, not by what was said.

`_run_speaker_facet` tallies "who discussed X" by scoping the whole tally
through `title` — app metadata the indexer writes, never something a
participant said. So "who discussed the migration" actually answers "who
attended a recording whose TITLE matches the migration": attendance, not
participation. Flag `chat.aggregate.speaker_facet_content_scope` (default
OFF) switches the same shape onto the chunk plane's `content.exact` field
instead, and excludes the unknown-speaker bucket from a content-scoped
answer (a title-scoped one keeps its pre-existing, unrelated behaviour).

Permission matrix row T7 lives here (the facet's own axis): LEAK (a
quarantined file's speaker must not survive into a content-scoped answer)
and SHARED (a genuinely shared file's speaker must survive it), each
exercised through the NEW content-scoped code path specifically — the
pre-existing quarantine coverage for the TITLE-scoped path already lives in
`test_chat_permissions_aggregation_quarantine.py` and is unmodified by this
change.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from typing import Any

import pytest

from app.services.chat.aggregation_service import answer_aggregation
from app.services.chat.router import route
from tests.unit.test_chat_aggregation import _RecordingClient

# Both W2.4 flags live in one SystemSettings key namespace
# ("chat.aggregate.*") — grouped with the sibling speaker_stats suite so
# concurrent xdist workers never race an INSERT on the same UNIQUE `key`
# (issue #389's documented hazard).
pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("chat_aggregate_speaker_flags")]

FACET_FLAG_KEY = "chat.aggregate.speaker_facet_content_scope"


@contextmanager
def _factory(db):
    yield db


def _sf(db_session):
    def _open():
        return _factory(db_session)

    return _open


def _set_flag(db, key: str, value: bool) -> None:
    from app.models.system_settings import SystemSettings

    db.add(SystemSettings(key=key, value="true" if value else "false"))
    db.commit()


def _make_file(db, user, *, title="Recording", quarantined=False):
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


def _share_with(db, owner, recipient, media_file, *, permission="viewer") -> None:
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    collection = Collection(
        user_id=owner.id, name=f"share-{uuid_pkg.uuid4().hex[:8]}", description="w2.4 test"
    )
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=recipient.id,
            permission=permission,
        )
    )
    db.commit()


def _people_agg_real_uuids(*pairs: tuple[str, int]) -> dict[str, Any]:
    """Like ``test_chat_aggregation._people_agg`` but with REAL uuid4 file
    ids instead of ``"{name}-{n}"`` placeholders — this module always runs
    against a real Postgres ``session_factory`` (the flag read needs one), so
    ``_quarantined_among``'s ``MediaFile.uuid IN (...)`` query would otherwise
    raise ``invalid input syntax for type uuid`` on the placeholder strings."""
    return {
        "aggregations": {
            "people": {
                "sum_other_doc_count": 0,
                "buckets": [
                    {
                        "key": name,
                        "files": {
                            "sum_other_doc_count": 0,
                            "buckets": [{"key": str(uuid_pkg.uuid4())} for _ in range(count)],
                        },
                    }
                    for name, count in pairs
                ],
            }
        }
    }


def _clause_field_names(body: dict[str, Any]) -> set[str]:
    """Field names the "mentions X" clause (the LAST filter clause) reads."""
    clause = body["query"]["bool"]["filter"][-1]
    should = clause["bool"]["should"]
    names: set[str] = set()
    for entry in should:
        if "match_phrase" in entry:
            names |= set(entry["match_phrase"])
        elif "match" in entry:
            names |= set(entry["match"])
    return names


QUESTION = "Which speakers discussed the migration?"


# ---------------------------------------------------------------------------
# Flag OFF: byte-identical to the pre-W2.4 title-scoped facet
# ---------------------------------------------------------------------------


def test_flag_off_is_byte_identical_to_the_pre_existing_title_scoped_facet(db_session, normal_user):
    client = _RecordingClient(_people_agg_real_uuids(("Ada", 2), ("Bo", 5)))

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert result.mechanism == "opensearch: title filter + terms(speakers) x terms(file_uuid)"
    assert result.coverage["scoped_by"] == "recording title (app metadata, not spoken content)"
    assert _clause_field_names(client.bodies[0]) == {"title"}
    assert (result.speaker, result.speaker_sessions) == ("Bo", 5)


def test_flag_off_control_the_unknown_bucket_is_not_excluded(db_session, normal_user):
    """The pre-existing behaviour this lane must NOT touch: a title-scoped
    facet has never excluded "Unknown Speaker" from the tally, and it can win
    if it has the most sessions. Proves the new exclusion is gated on the
    flag, not applied unconditionally."""
    client = _RecordingClient(_people_agg_real_uuids(("Unknown Speaker", 9), ("Ada", 2)))

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert result.speaker == "Unknown Speaker"


# ---------------------------------------------------------------------------
# Flag ON: content scope
# ---------------------------------------------------------------------------


def test_flag_on_scopes_by_spoken_content_not_the_recording_title(db_session, normal_user):
    _set_flag(db_session, FACET_FLAG_KEY, True)
    client = _RecordingClient(_people_agg_real_uuids(("Ada", 2), ("Bo", 5)))

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert (
        result.mechanism == "opensearch: content phrase filter + terms(speakers) x terms(file_uuid)"
    )
    assert result.coverage["scoped_by"] == "spoken content"
    assert _clause_field_names(client.bodies[0]) == {"content.exact"}


@pytest.mark.parametrize("unknown_label", ["Unknown Speaker", "Unknown"])
def test_flag_on_excludes_every_unknown_speaker_spelling(db_session, normal_user, unknown_label):
    _set_flag(db_session, FACET_FLAG_KEY, True)
    # The unknown bucket has far more sessions than Ada — if it were not
    # excluded it would win outright, not just appear in the rows.
    client = _RecordingClient(_people_agg_real_uuids((unknown_label, 9), ("Ada", 2)))

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert unknown_label not in dict(result.rows)
    assert result.speaker == "Ada"


def test_flag_on_declines_when_only_the_unknown_bucket_matched(db_session, normal_user):
    _set_flag(db_session, FACET_FLAG_KEY, True)
    client = _RecordingClient(_people_agg_real_uuids(("Unknown Speaker", 4)))

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is None


# ---------------------------------------------------------------------------
# T7 — permission matrix, exercised through the content-scoped path
# ---------------------------------------------------------------------------


def test_t7_leak_content_scoped_facet_excludes_a_quarantined_files_speaker(db_session, normal_user):
    _set_flag(db_session, FACET_FLAG_KEY, True)
    blocked = _make_file(db_session, normal_user, quarantined=True, title="Blocked")
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
                    }
                ],
            }
        }
    }
    client = _RecordingClient(people_agg)

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    # Ada's only session was the quarantined recording, dropped to zero — a
    # zero-session bucket is not a speaker in the answer at all.
    assert result is None


def test_t7_shared_content_scoped_facet_includes_a_shared_files_speaker(
    db_session, normal_user, other_user
):
    _set_flag(db_session, FACET_FLAG_KEY, True)
    shared = _make_file(db_session, other_user, title="Shared with me")
    _share_with(db_session, other_user, normal_user, shared)
    people_agg = {
        "aggregations": {
            "people": {
                "sum_other_doc_count": 0,
                "buckets": [
                    {
                        "key": "Ada",
                        "files": {
                            "sum_other_doc_count": 0,
                            "buckets": [{"key": str(shared.uuid)}],
                        },
                    }
                ],
            }
        }
    }
    client = _RecordingClient(people_agg)

    result = answer_aggregation(
        QUESTION,
        route(QUESTION),
        session_factory=_sf(db_session),
        client=client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert (result.speaker, result.speaker_sessions) == ("Ada", 1)
