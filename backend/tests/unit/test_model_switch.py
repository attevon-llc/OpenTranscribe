"""Tests for ``app/services/search/model_switch.py`` (issue #474).

The single implementation of the embedding-model switch (#437). Three things
are tested against REAL logic:

* ``dispatch_reindex_for_every_owner`` — against a REAL Postgres session
  (savepoint-isolated ``db_session``) with real ``MediaFile`` rows, so the
  "one coordinator per owner of a COMPLETED file, caller first" behavior is
  proven against the actual SQLAlchemy query, not a stand-in for it. The only
  patched seam is the Celery dispatch itself (``reindex_transcripts_task.delay``)
  — a real ``.delay()`` would reach the live dev-stack broker.
* ``apply_embedding_model_switch``'s two REFUSAL branches (unknown model /
  undeployed model) — these run before anything is written to OpenSearch or
  Postgres, so they need no OpenSearch at all. The branch that actually
  touches the ML Commons pipeline and recreates the index is intentionally
  NOT exercised here: doing so for real against the live dev-stack OpenSearch
  would delete its ``transcript_chunks`` index (see the module + package
  CLAUDE.md — ``recreate_index_for_dimension`` deletes the index), which is
  exactly the kind of live-data mutation this test suite must never risk.
* ``provenance_payload`` — a real ``EmbeddingProvenance`` instance (the
  actual dataclass, not a fake), proving the wire-shape mapping is right for
  every field including the computed ``message``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.search.embedding_provenance import EmbeddingProvenance
from app.services.search.model_switch import EmbeddingModelNotDeployedError
from app.services.search.model_switch import ReindexDispatchError
from app.services.search.model_switch import UnknownEmbeddingModelError
from app.services.search.model_switch import apply_embedding_model_switch
from app.services.search.model_switch import dispatch_reindex_for_every_owner
from app.services.search.model_switch import provenance_payload

_KNOWN_MODEL = next(iter(OPENSEARCH_EMBEDDING_MODELS))

pytestmark = pytest.mark.unit


# =============================================================================
# Helpers
# =============================================================================


@contextmanager
def _yield_session(db):
    """Stand-in for ``session_scope()`` handing out the test's savepoint session,
    matching the pattern documented/used in ``test_dispatch.py``."""
    yield db


def _make_media_file(db_session, user, status: FileStatus) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="model_switch_test.mp3",
        storage_path=f"model_switch/{uuid_pkg.uuid4()}.mp3",
        file_size=1024,
        content_type="audio/mpeg",
        status=status,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _baseline_completed_owner_ids(db_session) -> set[int]:
    """Owner ids that ALREADY have a COMPLETED file before this test adds any.

    This is a savepoint over the real dev-stack database (see ``backend/tests/CLAUDE.md``),
    so ``dispatch_reindex_for_every_owner``'s query sees whatever real completed files the
    live deployment already has, not just what this test creates. ``normal_user``/
    ``other_user`` are freshly inserted rows with brand-new ids, so they can never already
    be members of this set -- but asserting exact totals/dict-equality without accounting
    for this baseline is asserting a fact about the live database, not about the function.
    """
    rows = (
        db_session.query(MediaFile.user_id)
        .filter(MediaFile.status == FileStatus.COMPLETED)
        .distinct()
        .all()
    )
    return {int(r[0]) for r in rows}


# =============================================================================
# dispatch_reindex_for_every_owner
# =============================================================================


def test_dispatch_reindex_dispatches_the_triggering_user_even_with_no_files(
    db_session, normal_user
):
    """The settings UI keys its progress stream on the requesting user, so an
    admin who owns no COMPLETED files must still get a run to watch."""
    baseline = _baseline_completed_owner_ids(db_session)
    assert normal_user.id not in baseline  # sanity: a fresh user owns nothing yet

    fake_delay = MagicMock(side_effect=lambda **kw: MagicMock(id=f"task-{kw['user_id']}"))

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", fake_delay),
    ):
        result = dispatch_reindex_for_every_owner(triggered_by=normal_user.id)

    # The triggering user is always included, plus one coordinator per
    # pre-existing completed owner already in the database.
    assert result["reindex_users"] == len(baseline) + 1
    assert result["reindex_task_ids"][normal_user.id] == f"task-{normal_user.id}"
    assert set(result["reindex_task_ids"].keys()) == baseline | {normal_user.id}


def test_dispatch_reindex_covers_every_owner_of_a_completed_file_not_just_the_caller(
    db_session, normal_user, other_user
):
    """The #437 fix: the old code covered only the admin who pressed the button.
    A file owned by a DIFFERENT user must also get a coordinator."""
    baseline = _baseline_completed_owner_ids(db_session)
    _make_media_file(db_session, other_user, FileStatus.COMPLETED)

    fake_delay = MagicMock(side_effect=lambda **kw: MagicMock(id=f"task-{kw['user_id']}"))

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", fake_delay),
    ):
        result = dispatch_reindex_for_every_owner(triggered_by=normal_user.id)

    expected_owners = baseline | {normal_user.id, other_user.id}
    assert result["reindex_users"] == len(expected_owners)
    assert set(result["reindex_task_ids"].keys()) == expected_owners
    assert result["reindex_task_ids"][normal_user.id] == f"task-{normal_user.id}"
    assert result["reindex_task_ids"][other_user.id] == f"task-{other_user.id}"


def test_dispatch_reindex_triggering_user_is_dispatched_first(db_session, normal_user, other_user):
    """Ordering is load-bearing: the caller's own progress stream must start
    immediately, so it must be first regardless of user-id sort order."""
    _make_media_file(db_session, other_user, FileStatus.COMPLETED)
    _make_media_file(db_session, normal_user, FileStatus.COMPLETED)

    call_order: list[int] = []

    def _record(**kwargs):
        call_order.append(kwargs["user_id"])
        return MagicMock(id=f"task-{kwargs['user_id']}")

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", side_effect=_record),
    ):
        # Use the higher-numbered user as the trigger so "first" cannot be
        # explained by ascending id order alone.
        triggering_user = max(normal_user.id, other_user.id)
        dispatch_reindex_for_every_owner(triggered_by=triggering_user)

    assert call_order[0] == triggering_user
    assert {normal_user.id, other_user.id} <= set(call_order)
    # The trigger appears exactly once even though it also owns a completed file.
    assert call_order.count(triggering_user) == 1


def test_dispatch_reindex_deduplicates_multiple_completed_files_from_the_same_owner(
    db_session, normal_user
):
    baseline = _baseline_completed_owner_ids(db_session)
    _make_media_file(db_session, normal_user, FileStatus.COMPLETED)
    _make_media_file(db_session, normal_user, FileStatus.COMPLETED)
    _make_media_file(db_session, normal_user, FileStatus.COMPLETED)

    fake_delay = MagicMock(side_effect=lambda **kw: MagicMock(id=f"task-{kw['user_id']}"))

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", fake_delay),
    ):
        result = dispatch_reindex_for_every_owner(triggered_by=normal_user.id)

    # DISTINCT on user_id: three completed files from one owner still means
    # exactly one coordinator dispatch for that owner.
    assert result["reindex_users"] == len(baseline | {normal_user.id})
    dispatches_for_normal_user = [
        call for call in fake_delay.call_args_list if call.kwargs["user_id"] == normal_user.id
    ]
    assert len(dispatches_for_normal_user) == 1


def test_dispatch_reindex_ignores_owners_whose_files_are_not_completed(
    db_session, normal_user, other_user
):
    baseline = _baseline_completed_owner_ids(db_session)
    _make_media_file(db_session, other_user, FileStatus.PROCESSING)
    _make_media_file(db_session, other_user, FileStatus.ERROR)

    fake_delay = MagicMock(side_effect=lambda **kw: MagicMock(id=f"task-{kw['user_id']}"))

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", fake_delay),
    ):
        result = dispatch_reindex_for_every_owner(triggered_by=normal_user.id)

    # other_user owns no COMPLETED file, so is not a coordinator target --
    # only the triggering user plus whatever was already in the database.
    assert other_user.id not in result["reindex_task_ids"]
    assert set(result["reindex_task_ids"].keys()) == baseline | {normal_user.id}


def test_dispatch_reindex_with_a_named_scope_covers_exactly_those_owners(
    db_session, normal_user, other_user
):
    """The #627 partial scope: each owner's coordinator gets that owner's files.

    A partial scope must NOT also sweep in every other owner of a completed file
    the way the default whole-corpus mode does — a pending-only sweep that
    re-embedded fully-indexed accounts is the opposite defect.
    """
    _make_media_file(db_session, other_user, FileStatus.COMPLETED)
    fake_delay = MagicMock(side_effect=lambda **kw: MagicMock(id=f"task-{kw['user_id']}"))

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", fake_delay),
    ):
        result = dispatch_reindex_for_every_owner(
            triggered_by=normal_user.id,
            file_uuids_by_owner={other_user.id: ["uuid-a", "uuid-b"]},
        )

    assert set(result["reindex_task_ids"].keys()) == {other_user.id}
    fake_delay.assert_called_once_with(user_id=other_user.id, file_uuids=["uuid-a", "uuid-b"])


def test_dispatch_reindex_drops_a_named_owner_with_an_empty_file_list(
    db_session, normal_user, other_user
):
    """``file_uuids=[]`` would re-embed that owner's WHOLE account, not nothing.

    ``reindex_transcripts_task`` narrows its snapshot with ``if file_uuids:``, so a
    falsy list is indistinguishable from "no filter given". An owner the scope
    resolved to zero pending files must therefore be dropped, never dispatched
    with an empty list.
    """
    _make_media_file(db_session, other_user, FileStatus.COMPLETED)
    fake_delay = MagicMock(side_effect=lambda **kw: MagicMock(id=f"task-{kw['user_id']}"))

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", fake_delay),
    ):
        result = dispatch_reindex_for_every_owner(
            triggered_by=normal_user.id,
            file_uuids_by_owner={other_user.id: [], normal_user.id: ["uuid-a"]},
        )

    assert set(result["reindex_task_ids"].keys()) == {normal_user.id}
    fake_delay.assert_called_once_with(user_id=normal_user.id, file_uuids=["uuid-a"])


def test_dispatch_reindex_raises_when_delay_fails_and_message_reports_partial_progress(
    db_session, normal_user, other_user
):
    """A failed dispatch is intentionally NOT caught per-user (see the module
    docstring) — the whole switch is already applied by the time this runs, so
    swallowing the failure would silently under-report a mixed vector space."""
    baseline = _baseline_completed_owner_ids(db_session)
    _make_media_file(db_session, other_user, FileStatus.COMPLETED)
    total_owners = len(baseline | {normal_user.id, other_user.id})

    call_count = 0

    def _boom(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MagicMock(id="task-first")
        raise RuntimeError("Retry limit exceeded while trying to reconnect to the broker")

    with (
        patch("app.db.session_utils.session_scope", lambda: _yield_session(db_session)),
        patch("app.tasks.reindex_task.reindex_transcripts_task.delay", side_effect=_boom),
    ):
        with pytest.raises(ReindexDispatchError) as exc_info:
            dispatch_reindex_for_every_owner(triggered_by=normal_user.id)

    message = str(exc_info.value)
    assert f"1 of {total_owners} users" in message
    assert "Retry limit exceeded" in message


# =============================================================================
# apply_embedding_model_switch — refusal branches (no OpenSearch required)
# =============================================================================


def test_apply_embedding_model_switch_rejects_a_model_not_in_the_registry():
    with pytest.raises(UnknownEmbeddingModelError, match="not-a-real-model"):
        apply_embedding_model_switch("not-a-real-model", triggered_by=1)


def test_apply_embedding_model_switch_rejects_when_model_is_not_registered_in_opensearch():
    """find_model_by_name() returns None -> refused BEFORE any settings/pipeline
    write, per the module docstring's ordering guarantee."""
    fake_ml_service = MagicMock()
    fake_ml_service.find_model_by_name.return_value = None

    with (
        patch(
            "app.services.search.ml_model_service.get_ml_model_service",
            return_value=fake_ml_service,
        ),
        patch("app.services.search.settings_service.save_search_embedding_model") as save_setting,
    ):
        with pytest.raises(EmbeddingModelNotDeployedError, match=_KNOWN_MODEL):
            apply_embedding_model_switch(_KNOWN_MODEL, triggered_by=1)

    save_setting.assert_not_called()


def test_apply_embedding_model_switch_rejects_when_model_is_registered_but_not_deployed():
    """find_model_by_name() finds an id, but get_model_status says it is not
    deployed -> still refused, still before any write."""
    fake_ml_service = MagicMock()
    fake_ml_service.find_model_by_name.return_value = "ml-model-id-123"
    fake_ml_service.get_model_status.return_value = {"deployed": False}

    with (
        patch(
            "app.services.search.ml_model_service.get_ml_model_service",
            return_value=fake_ml_service,
        ),
        patch("app.services.search.settings_service.save_search_embedding_model") as save_setting,
    ):
        with pytest.raises(EmbeddingModelNotDeployedError):
            apply_embedding_model_switch(_KNOWN_MODEL, triggered_by=1)

    fake_ml_service.get_model_status.assert_called_once_with("ml-model-id-123")
    save_setting.assert_not_called()


# =============================================================================
# provenance_payload
# =============================================================================


def test_provenance_payload_serializes_a_mixed_survey_exactly():
    survey = EmbeddingProvenance(
        verdict="mixed",
        counts={"model-a": 10, "model-b": 5, "__unknown__": 2},
        known_models=("model-a", "model-b"),
        total=17,
        unattributed=2,
        no_embedding=0,
        mixed=True,
    )

    payload = provenance_payload(survey)

    assert payload == {
        "verdict": "mixed",
        "mixed": True,
        "comparable": False,
        "models": ["model-a", "model-b"],
        "counts": {"model-a": 10, "model-b": 5, "__unknown__": 2},
        "total": 17,
        "unattributed": 2,
        "no_embedding": 0,
        "message": survey.describe(),
    }
    assert "MIXED VECTOR SPACE" in payload["message"]


def test_provenance_payload_serializes_a_uniform_survey_as_comparable():
    survey = EmbeddingProvenance(
        verdict="uniform",
        counts={"model-a": 42},
        known_models=("model-a",),
        total=42,
        unattributed=0,
        no_embedding=0,
        mixed=False,
    )

    payload = provenance_payload(survey)

    assert payload["comparable"] is True
    assert payload["mixed"] is False
    assert payload["models"] == ["model-a"]
    assert "42 documents were embedded by model-a" in payload["message"]


def test_provenance_payload_models_field_is_a_plain_list_not_a_tuple():
    """`models` must be JSON-safe; the dataclass field is a tuple internally."""
    survey = EmbeddingProvenance(
        verdict="empty", counts={}, known_models=(), total=0, unattributed=0, no_embedding=0
    )

    payload = provenance_payload(survey)

    assert isinstance(payload["models"], list)
