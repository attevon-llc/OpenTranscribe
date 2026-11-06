"""The topic-extraction response parser and the two writers around it, each tested by name.

``_parse_llm_response`` decides whether a model's reply becomes a suggestion at all,
``_store_suggestion`` writes it, and ``apply_suggestions`` records what the user accepted. All
three were reached only through other suites, so a regression in one would have been blamed on
whichever caller happened to trip over it.

The parser tests need no database; the writer tests are DB-backed, so run the file through
``scripts/run-backend-tests.sh --throwaway-db``.
"""

from __future__ import annotations

import json
import uuid as uuid_mod
from unittest import mock

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.topic import TopicSuggestion
from app.schemas.topic import LLMSuggestionResponse
from app.schemas.topic import SuggestedCollection
from app.schemas.topic import SuggestedTag
from app.services.llm_types import LLMProvider
from app.services.topic_extraction_service import TopicExtractionService

pytestmark = pytest.mark.unit

PAYLOAD = {
    "suggested_collections": [{"name": "Team Standups", "confidence": 0.9, "rationale": "r"}],
    "suggested_tags": [
        {"name": "budget", "confidence": 0.9, "rationale": "r"},
        {"name": "hiring", "confidence": 0.8},
    ],
}


def _parse(text: str, provider: LLMProvider = LLMProvider.CUSTOM):
    return TopicExtractionService()._parse_llm_response(text, provider)


# ------------------------------------------------------------------------------ the parser


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            f"<thinking>x</thinking>\n<answer>\n{json.dumps(PAYLOAD)}\n</answer>", id="answer-tags"
        ),
        pytest.param(json.dumps(PAYLOAD), id="bare-json"),
        pytest.param(
            f"Here you go:\n```json\n{json.dumps(PAYLOAD)}\n```\nHope that helps.", id="in-prose"
        ),
        pytest.param(json.dumps(PAYLOAD, indent=2), id="pretty-printed"),
    ],
)
def test_a_reply_carrying_the_json_is_parsed(reply):
    parsed = _parse(reply)

    assert parsed is not None
    assert [t.name for t in parsed.suggested_tags] == ["budget", "hiring"]
    assert [c.name for c in parsed.suggested_collections] == ["Team Standups"]
    assert parsed.suggested_tags[1].rationale is None, "rationale is optional"


def test_the_answer_tags_win_over_a_stray_object_earlier_in_the_reply():
    reply = f'<thinking>maybe {{"a": 1}}</thinking><answer>{json.dumps(PAYLOAD)}</answer>'

    parsed = _parse(reply)

    assert parsed is not None and len(parsed.suggested_tags) == 2


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("", id="empty"),
        pytest.param("I could not find any topics worth suggesting.", id="no-json"),
        pytest.param('{"suggested_tags": [{"name": "budget", "confidence": 0.9', id="truncated"),
        pytest.param(
            json.dumps({"suggested_tags": PAYLOAD["suggested_tags"]}), id="missing-collections"
        ),
        pytest.param(
            json.dumps({**PAYLOAD, "suggested_tags": [{"confidence": 0.9}]}), id="tag-without-name"
        ),
        pytest.param(
            json.dumps({**PAYLOAD, "suggested_tags": [{"name": "budget", "confidence": 1.5}]}),
            id="confidence-above-one",
        ),
        pytest.param(
            json.dumps({**PAYLOAD, "suggested_tags": [{"name": "budget", "confidence": -0.1}]}),
            id="confidence-below-zero",
        ),
        pytest.param("[1, 2, 3]", id="json-array-not-object"),
    ],
)
def test_a_reply_that_is_not_a_valid_suggestion_is_rejected_not_repaired(reply):
    assert _parse(reply) is None


# ------------------------------------------------------------------------------ the writers


@pytest.fixture
def media_file(db_session, normal_user) -> MediaFile:
    mf = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=normal_user.id,
        filename="standup.wav",
        storage_path=f"test/standup-{uuid_mod.uuid4().hex}.wav",
        content_type="audio/wav",
        file_size=1000,
        status=FileStatus.COMPLETED,
    )
    db_session.add(mf)
    db_session.commit()
    return mf


def _response(*tags: tuple[str, float], collection: str = "Team Standups") -> LLMSuggestionResponse:
    return LLMSuggestionResponse(
        suggested_collections=[
            SuggestedCollection(name=collection, confidence=0.9, rationale=None)
        ],
        suggested_tags=[
            SuggestedTag(name=name, confidence=conf, rationale=None) for name, conf in tags
        ],
    )


@pytest.fixture(autouse=True)
def _no_auto_apply(monkeypatch):
    """Auto-applying tags is a separate feature with its own suites; keep it out of these."""
    monkeypatch.setattr(
        "app.services.auto_label_settings.get_user_auto_label_settings",
        lambda session, user_id: {"enabled": False},
    )


def test_store_suggestion_creates_one_pending_row_owned_by_the_files_owner(db_session, media_file):
    service = TopicExtractionService(db_session)

    stored = service._store_suggestion(media_file, _response(("budget", 0.9), ("hiring", 0.6)))

    assert stored is not None
    assert (stored.media_file_id, stored.user_id, stored.status) == (
        media_file.id,
        media_file.user_id,
        "pending",
    )
    assert stored.suggested_tags == [
        {"name": "budget", "confidence": 0.9, "rationale": None},
        {"name": "hiring", "confidence": 0.6, "rationale": None},
    ]
    assert stored.suggested_collections == [
        {"name": "Team Standups", "confidence": 0.9, "rationale": None}
    ]


def test_store_suggestion_replaces_the_previous_suggestion_and_reopens_it(db_session, media_file):
    service = TopicExtractionService(db_session)
    first = service._store_suggestion(media_file, _response(("budget", 0.9)))
    assert first is not None
    first.status = "accepted"
    db_session.commit()

    second = service._store_suggestion(
        media_file, _response(("roadmap", 0.8), collection="Planning")
    )

    assert second is not None and second.id == first.id, "a second row was created"
    assert second.status == "pending", "a regenerated suggestion must be offered again"
    assert [t["name"] for t in second.suggested_tags or []] == ["roadmap"]
    assert [c["name"] for c in second.suggested_collections or []] == ["Planning"]
    count = db_session.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == media_file.id)
    assert count.count() == 1


def test_store_suggestion_returns_none_and_rolls_back_when_the_write_fails(db_session, media_file):
    service = TopicExtractionService(db_session)

    # rollback is spied on rather than run: on the shared test connection a real rollback
    # would also discard the rows the fixtures committed.
    with (
        mock.patch.object(db_session, "commit", side_effect=RuntimeError("disk full")),
        mock.patch.object(db_session, "rollback") as rollback,
    ):
        stored = service._store_suggestion(media_file, _response(("budget", 0.9)))

    assert stored is None
    rollback.assert_called_once_with()


def test_a_failing_auto_apply_never_loses_the_stored_suggestion(
    db_session, media_file, monkeypatch
):
    monkeypatch.setattr(
        "app.services.auto_label_settings.get_user_auto_label_settings",
        lambda session, user_id: {"enabled": True, "confidence_threshold": 0.75},
    )
    service = TopicExtractionService(db_session)

    with mock.patch(
        "app.services.auto_label_service.AutoLabelService.auto_apply_suggestions",
        side_effect=RuntimeError("tag service down"),
    ):
        stored = service._store_suggestion(media_file, _response(("budget", 0.9)))

    assert stored is not None
    persisted = (
        db_session.query(TopicSuggestion)
        .filter(TopicSuggestion.media_file_id == media_file.id)
        .one()
    )
    assert [t["name"] for t in persisted.suggested_tags] == ["budget"]


@pytest.fixture
def stored_suggestion(db_session, media_file) -> TopicSuggestion:
    stored = TopicExtractionService(db_session)._store_suggestion(
        media_file, _response(("budget", 0.9), ("hiring", 0.8))
    )
    assert stored is not None
    return stored


def test_apply_suggestions_records_what_was_accepted_and_keeps_the_suggestion_open(
    db_session, stored_suggestion
):
    service = TopicExtractionService(db_session)

    ok = service.apply_suggestions(stored_suggestion.id, ["Team Standups"], ["budget"])

    assert ok is True
    db_session.expire_all()
    row = db_session.get(TopicSuggestion, stored_suggestion.id)
    assert row.user_decisions == {
        "accepted_collections": ["Team Standups"],
        "accepted_tags": ["budget"],
    }
    assert row.status == "pending", "the remaining suggestions must stay available"


def test_apply_suggestions_accumulates_across_calls_without_duplicates(
    db_session, stored_suggestion
):
    service = TopicExtractionService(db_session)
    service.apply_suggestions(stored_suggestion.id, ["Team Standups"], ["budget"])

    service.apply_suggestions(stored_suggestion.id, [], ["hiring", "budget"])

    db_session.expire_all()
    decisions = db_session.get(TopicSuggestion, stored_suggestion.id).user_decisions
    assert sorted(decisions["accepted_tags"]) == ["budget", "hiring"]
    assert decisions["accepted_collections"] == ["Team Standups"]


def test_apply_suggestions_on_an_unknown_id_is_false_and_writes_nothing(db_session):
    assert (
        TopicExtractionService(db_session).apply_suggestions(2_000_000_000, ["x"], ["y"]) is False
    )


def test_apply_suggestions_reports_false_and_rolls_back_when_the_commit_fails(
    db_session, stored_suggestion
):
    service = TopicExtractionService(db_session)

    with (
        mock.patch.object(db_session, "commit", side_effect=RuntimeError("deadlock")),
        mock.patch.object(db_session, "rollback") as rollback,
    ):
        ok = service.apply_suggestions(stored_suggestion.id, ["Team Standups"], ["budget"])

    assert ok is False
    rollback.assert_called_once_with()
