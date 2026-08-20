"""W2.4 fix #2: `SHAPE_SPEAKER_STATS` — "who talked the most" from exact per-speaker
talk time in ``file_facts.facts['speakers']``, gated by
``chat.aggregate.speaker_stats_enabled`` (default OFF).

The gap this closes: exact per-speaker talk time already sits in
``file_facts`` (`services/ingest_artifacts/facts.py`) and nothing read it.
"Who talked the most" was previously only answerable by the attendance-style
speaker facet (`SHAPE_SPEAKER_FACET`) — a different question with a
different mechanism. `router.py`'s new `who-talked-most` signal is kept
distinct from the existing `who-most` signal, and `aggregation.choose_shape`
gives it priority so the two questions cannot collapse into one route.

Permission matrix row T6 lives here (mirrors the pattern
`test_chat_permissions_aggregation.py` already established for
`_occurrence_count`/`_files_in_period`): LEAK (a personal-scope answer must
exclude the caller's own org-stamped file) and SHARED (a bounded scope
spanning an owned file and a genuinely shared one must tally both).
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg
from contextlib import contextmanager
from typing import Any

import pytest

from app.services.chat.aggregation import SHAPE_SPEAKER_FACET
from app.services.chat.aggregation import SHAPE_SPEAKER_STATS
from app.services.chat.aggregation import choose_shape
from app.services.chat.aggregation_service import answer_aggregation
from app.services.chat.router import route
from tests.unit.test_chat_aggregation import _RecordingClient

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("chat_aggregate_speaker_flags")]

STATS_FLAG_KEY = "chat.aggregate.speaker_stats_enabled"


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


@pytest.fixture
def org(db_session):
    from app.models.organization import Organization

    organization = Organization(
        uuid=uuid_pkg.uuid4(),
        external_org_id=f"org-w24-{uuid_pkg.uuid4().hex[:8]}",
        name="W2.4 Org",
        is_active=True,
    )
    db_session.add(organization)
    db_session.commit()
    db_session.refresh(organization)
    return organization


def _make_file(db, user, *, title="Recording", organization_id=None):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=organization_id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
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


def _speaker_entry(name: str, total_time: float, **overrides: Any) -> dict[str, Any]:
    entry = {
        "name": name,
        "total_time": total_time,
        "segment_count": 1,
        "word_count": 10,
        "percentage": 100.0,
        "turn_count": 1,
        "longest_turn": total_time,
    }
    entry.update(overrides)
    return entry


def _people_agg_real_uuids(*pairs: tuple[str, int]) -> dict[str, Any]:
    """Like ``test_chat_aggregation._people_agg`` but with REAL uuid4 file
    ids — this module runs against a real Postgres ``session_factory``, so a
    quarantine or flag-fallback check that touches ``MediaFile.uuid`` would
    otherwise raise ``invalid input syntax for type uuid`` on placeholders."""
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


def _make_facts(db, media_file, *, speakers: list[dict[str, Any]], undiarized: int = 0):
    from app.models.file_facts import FileFacts

    row = FileFacts(
        media_file_id=media_file.id,
        generator_version="1.1.1",
        source_fingerprint=uuid_pkg.uuid4().hex + uuid_pkg.uuid4().hex,
        facts={
            "speakers": speakers,
            "coverage": {"undiarized_files_excluded": undiarized},
        },
        digest={"sections": []},
        keyphrases={},
        digest_word_count=0,
        section_count=0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


TALKED_QUESTION = "Who talked the most?"


# ---------------------------------------------------------------------------
# router.py: `who-talked-most` is a distinct signal from `who-most`
# ---------------------------------------------------------------------------


def test_who_talked_most_is_a_distinct_signal_from_who_most():
    talked = route(TALKED_QUESTION)
    attended = route("Who attended the most design review sessions?")

    assert "who-talked-most" in talked.signals
    assert "who-talked-most" not in attended.signals


def test_who_talked_most_and_who_most_choose_different_shapes():
    """The property that matters: these are different questions and must not
    collapse into one route, even though both signals can fire on the same
    text (see router.py's `who-talked-most` docstring)."""
    talked = route(TALKED_QUESTION)
    attended = route("Who attended the most design review sessions?")

    assert choose_shape(talked) == SHAPE_SPEAKER_STATS
    assert choose_shape(attended) == SHAPE_SPEAKER_FACET
    assert choose_shape(talked) != choose_shape(attended)


def test_a_talk_time_phrase_with_no_who_still_fires_the_signal():
    """The alternate phrasing that carries no `who` at all — proves the
    signal is not accidentally anchored to `who-most`'s "who ... the most"
    shape."""
    decision = route("Most talk time?")
    assert "who-talked-most" in decision.signals
    assert "who-most" not in decision.signals
    assert choose_shape(decision) == SHAPE_SPEAKER_STATS


@pytest.mark.parametrize(
    "question",
    [
        "What did Alice say about the migration?",
        "Tell me what Bob talked about in the standup.",
        "Who is Alice talking to about the budget?",
    ],
)
def test_lookup_leakage_pin_ordinary_questions_never_fire_the_new_signal(question):
    """The router's "removes, never promotes" property, from this signal's
    side: an ordinary lookup about what someone said must not be rerouted."""
    decision = route(question)
    assert "who-talked-most" not in decision.signals
    assert decision.intent == "lookup"


def test_route_carries_the_active_speaker_scope_for_the_stats_shape_to_read():
    decision = route(TALKED_QUESTION, speakers=["Alice", "Bob"])
    assert decision.speakers == ("Alice", "Bob")


def test_route_speakers_defaults_to_empty():
    assert route(TALKED_QUESTION).speakers == ()


# ---------------------------------------------------------------------------
# Flag OFF: byte-identical to the pre-existing behaviour
# ---------------------------------------------------------------------------


def test_flag_off_falls_back_to_the_pre_existing_facet_shape(db_session, normal_user):
    """ "Who talked the most" already satisfied `who-most` before this shape
    existed, so with the flag off it must still be answered exactly that way
    — same shape, same mechanism string, same title-scoped tally."""
    facet_client = _RecordingClient(_people_agg_real_uuids(("Ada", 2), ("Bo", 5)))

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=facet_client,
        index="i",
        user_id=normal_user.id,
    )

    assert result is not None
    assert result.shape == SHAPE_SPEAKER_FACET
    assert result.mechanism == "opensearch: title filter + terms(speakers) x terms(file_uuid)"
    assert (result.speaker, result.speaker_sessions) == ("Bo", 5)


def test_flag_off_with_no_fallback_signal_declines_entirely(db_session, normal_user):
    """A phrasing that fires ONLY `who-talked-most` (no `who-most` overlap)
    must decline cleanly with the flag off — exactly as it did before this
    shape existed, when nothing recognised the phrase at all."""
    result = answer_aggregation(
        "Most talk time?",
        route("Most talk time?"),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
    )

    assert result is None


# ---------------------------------------------------------------------------
# Flag ON — the shape itself
# ---------------------------------------------------------------------------


def test_flag_on_answers_from_exact_talk_time(db_session, normal_user):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    quiet = _make_file(db_session, normal_user, title="Quiet")
    _make_facts(db_session, quiet, speakers=[_speaker_entry("Alice", 50.0)])
    loud = _make_file(db_session, normal_user, title="Loud")
    _make_facts(db_session, loud, speakers=[_speaker_entry("Bob", 500.0)])

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(quiet.uuid), str(loud.uuid)],
    )

    assert result is not None
    assert result.shape == SHAPE_SPEAKER_STATS
    assert result.speaker == "Bob"
    assert result.speaker_seconds == 500.0
    assert result.count == 2


def test_attended_most_and_talked_most_give_different_correct_answers(db_session, normal_user):
    """The test that proves the whole lane was worth doing: without it, both
    shapes could be silently returning attendance and nothing would notice.

    Alice appears in three short standups (attendance-heavy); Bob appears in
    two long deep-dives where he talks far more overall (talk-time-heavy).
    "Who attended the most" and "who talked the most" are different
    questions with different correct answers over the SAME corpus.
    """
    _set_flag(db_session, STATS_FLAG_KEY, True)

    alice_files = [_make_file(db_session, normal_user, title=f"Standup {n}") for n in range(3)]
    for f in alice_files:
        _make_facts(db_session, f, speakers=[_speaker_entry("Alice", 60.0)])

    bob_files = [_make_file(db_session, normal_user, title=f"Deep Dive {n}") for n in range(2)]
    for f in bob_files:
        _make_facts(db_session, f, speakers=[_speaker_entry("Bob", 1800.0)])

    all_uuids = [str(f.uuid) for f in [*alice_files, *bob_files]]

    facet_client = _RecordingClient(
        {
            "aggregations": {
                "people": {
                    "sum_other_doc_count": 0,
                    "buckets": [
                        {
                            "key": "Alice",
                            "files": {
                                "sum_other_doc_count": 0,
                                "buckets": [{"key": str(f.uuid)} for f in alice_files],
                            },
                        },
                        {
                            "key": "Bob",
                            "files": {
                                "sum_other_doc_count": 0,
                                "buckets": [{"key": str(f.uuid)} for f in bob_files],
                            },
                        },
                    ],
                }
            }
        }
    )
    attendance_question = "Who attended the most design review sessions?"
    attendance = answer_aggregation(
        attendance_question,
        route(attendance_question),
        session_factory=_sf(db_session),
        client=facet_client,
        index="i",
        user_id=normal_user.id,
        file_uuids=all_uuids,
    )
    assert attendance is not None
    assert attendance.shape == SHAPE_SPEAKER_FACET
    assert (attendance.speaker, attendance.speaker_sessions) == ("Alice", 3)

    talked = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=all_uuids,
    )
    assert talked is not None
    assert talked.shape == SHAPE_SPEAKER_STATS
    assert (talked.speaker, talked.speaker_seconds) == ("Bob", 3600.0)

    # The point made explicit: same corpus, same bounded scope, opposite
    # "top speaker" depending on which question was actually asked.
    assert attendance.speaker != talked.speaker


def test_unbounded_scope_declines_with_a_disclosure(db_session, normal_user):
    _set_flag(db_session, STATS_FLAG_KEY, True)

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=None,
    )

    assert result is not None
    assert result.count is None
    assert result.speaker is None
    assert "bounded" in str(result.coverage.get("declined", ""))


def test_partial_facts_coverage_declines_rather_than_reporting_a_partial_tally(
    db_session, normal_user
):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    complete = _make_file(db_session, normal_user, title="Has facts")
    _make_facts(db_session, complete, speakers=[_speaker_entry("Alice", 100.0)])
    incomplete = _make_file(db_session, normal_user, title="No facts yet")  # no FileFacts row

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(complete.uuid), str(incomplete.uuid)],
    )

    assert result is not None
    assert result.count is None
    assert result.speaker is None
    assert result.coverage["files_without_artifacts"] == 1


def test_a_tie_at_the_top_is_refused_not_broken_arbitrarily(db_session, normal_user):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    a = _make_file(db_session, normal_user, title="A")
    _make_facts(db_session, a, speakers=[_speaker_entry("Alice", 100.0)])
    b = _make_file(db_session, normal_user, title="B")
    _make_facts(db_session, b, speakers=[_speaker_entry("Bob", 100.0)])

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(a.uuid), str(b.uuid)],
    )

    assert result is not None
    assert result.speaker is None
    assert result.coverage["tied_at_top"] == 2


def test_speaker_focus_narrows_the_result_to_one_name(db_session, normal_user):
    """Even though Bob talked more overall, focusing the turn on Alice must
    report ALICE'S time, not silently redirect to whoever talked most."""
    _set_flag(db_session, STATS_FLAG_KEY, True)
    f = _make_file(db_session, normal_user, title="Mixed")
    _make_facts(
        db_session, f, speakers=[_speaker_entry("Alice", 100.0), _speaker_entry("Bob", 500.0)]
    )

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION, speakers=["Alice"]),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(f.uuid)],
    )

    assert result is not None
    assert result.speaker == "Alice"
    assert result.speaker_seconds == 100.0
    assert result.count == 1


def test_a_focused_speaker_absent_from_scope_declines_with_disclosure(db_session, normal_user):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    f = _make_file(db_session, normal_user, title="Solo")
    _make_facts(db_session, f, speakers=[_speaker_entry("Bob", 500.0)])

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION, speakers=["Zara"]),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(f.uuid)],
    )

    assert result is not None
    assert result.speaker is None
    assert result.count is None
    assert result.coverage["speaker_not_found"] == "Zara"


def test_an_unknown_labeled_entry_is_defensively_excluded_from_totals(db_session, normal_user):
    """`file_facts.facts['speakers']` should never contain the unknown
    bucket (`build_facts` excludes it at ingest) — this proves the shape does
    not TRUST that and defensively excludes it anyway, even when it would
    otherwise dominate the tally."""
    _set_flag(db_session, STATS_FLAG_KEY, True)
    f = _make_file(db_session, normal_user, title="Mixed")
    _make_facts(
        db_session,
        f,
        speakers=[_speaker_entry("Unknown Speaker", 999.0), _speaker_entry("Alice", 50.0)],
    )

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(f.uuid)],
    )

    assert result is not None
    assert result.speaker == "Alice"


def test_undiarized_files_are_excluded_and_reported(db_session, normal_user):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    undiarized = _make_file(db_session, normal_user, title="No diarization")
    _make_facts(db_session, undiarized, speakers=[], undiarized=1)
    good = _make_file(db_session, normal_user, title="Diarized fine")
    _make_facts(db_session, good, speakers=[_speaker_entry("Alice", 100.0)])

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        file_uuids=[str(undiarized.uuid), str(good.uuid)],
    )

    assert result is not None
    assert result.speaker == "Alice"
    assert result.coverage["undiarized_files_excluded"] == 1


# ---------------------------------------------------------------------------
# T6 — permission matrix
# ---------------------------------------------------------------------------


def test_t6_leak_personal_scope_excludes_the_callers_own_org_stamped_file(
    db_session, normal_user, org
):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    personal = _make_file(db_session, normal_user, title="Personal", organization_id=None)
    _make_facts(db_session, personal, speakers=[_speaker_entry("Alice", 100.0)])
    org_file = _make_file(db_session, normal_user, title="OrgOnly", organization_id=org.id)
    _make_facts(db_session, org_file, speakers=[_speaker_entry("Bob", 999.0)])

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        organization_id=None,
        file_uuids=[str(personal.uuid), str(org_file.uuid)],
    )

    assert result is not None
    # Bob's 999s sits in an org-stamped file the same user owns — it must not
    # leak into a PERSONAL-scope answer just because ownership matches.
    assert result.speaker == "Alice"


def test_t6_shared_stats_span_owned_and_shared_files_in_one_bounded_scope(
    db_session, normal_user, other_user
):
    _set_flag(db_session, STATS_FLAG_KEY, True)
    own = _make_file(db_session, normal_user, title="Mine")
    _make_facts(db_session, own, speakers=[_speaker_entry("Alice", 50.0)])
    shared = _make_file(db_session, other_user, title="Shared with me")
    _make_facts(db_session, shared, speakers=[_speaker_entry("Bob", 500.0)])
    _share_with(db_session, other_user, normal_user, shared)

    result = answer_aggregation(
        TALKED_QUESTION,
        route(TALKED_QUESTION),
        session_factory=_sf(db_session),
        client=_RecordingClient(),
        index="i",
        user_id=normal_user.id,
        organization_id=None,
        file_uuids=[str(own.uuid), str(shared.uuid)],
    )

    assert result is not None
    assert result.speaker == "Bob"
    assert result.speaker_seconds == 500.0
    # Both the owned file and the genuinely shared one made it into the
    # tally — a vacuous share fixture would still pass with count == 1.
    assert result.count == 2


# ---------------------------------------------------------------------------
# The <counted> block renders talk time, not a bogus "0 recordings"
# ---------------------------------------------------------------------------


def test_the_counted_block_renders_talk_time_not_session_count():
    """`speaker_seconds` and `speaker_sessions` are different units of the
    same "top speaker" idea and must never both render — the facet's
    "(N recordings)" phrasing would misreport a talk-time result as having
    appeared in zero recordings."""
    from app.services.chat.aggregation import AggregationResult
    from app.services.chat.prompting import format_counted_block

    block = format_counted_block(
        AggregationResult(
            shape=SHAPE_SPEAKER_STATS,
            mechanism="postgres: file_facts.facts['speakers']",
            subject="",
            count=2,
            speaker="Bob",
            speaker_seconds=742.3,
        )
    )

    assert "top speaker: Bob" in block
    assert "talk time" in block
    assert "recordings)" not in block


def test_the_counted_block_reports_a_disclosure_with_no_speaker_or_count():
    """Base rule 10 tells the model to report a `<counted>` limitation, so a
    refusal (unbounded scope, partial coverage, a tie) must still produce a
    block the model can read — not an empty one."""
    from app.services.chat.aggregation import AggregationResult
    from app.services.chat.prompting import format_counted_block

    block = format_counted_block(
        AggregationResult(
            shape=SHAPE_SPEAKER_STATS,
            mechanism="postgres: file_facts.facts['speakers']",
            subject="",
            coverage={"declined": "talk-time stats need a bounded set of recordings"},
        )
    )

    assert "<counted>" in block
    assert "total:" not in block
    assert "top speaker:" not in block
    assert "declined" in block
