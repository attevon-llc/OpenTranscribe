"""Background work skips a frozen file and writes nothing (legal-hold freeze, plan §3.1, T1-T8).

A file is FROZEN when it is held and its initial processing run has finished. Work dispatched
after the run (a summary, topics, LLM speaker identification, speaker attributes, an auto-label,
a redaction RESCAN, a manual speaker re-detection) is skipped with ``skipped_legal_hold``; a file
held while it is still ``processing`` is not frozen, so the run in flight finishes. Sweeps that
select files (the redaction reindex, the auto-label backlog, the attribute reset, cross-file
voice matching) leave held files out with ``not_held_pred()``.

Against the real database: the tasks' own ``session_scope`` is pointed at the test session, and
every step past the guard is replaced by a tripwire, so "skipped" cannot be a step that silently
did nothing.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.topic import TopicSuggestion
from app.services.hold_guard import frozen_states
from app.tasks.hold_skip import SKIPPED_LEGAL_HOLD


@pytest.fixture
def scoped(db_session, monkeypatch):
    from app.db import session_utils

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(session_utils, "session_scope", _scope)
    # The legal processing gate is a different backstop; it passes for these files.
    monkeypatch.setattr("app.legal.processing_gate.refusal_for_file", lambda **kw: None)
    return db_session


def _file(db, owner, *, held: bool, status: str = "completed", **extra) -> MediaFile:
    row = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="held.wav",
        title="held",
        storage_path=f"media/test/{uuid.uuid4()}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=status,
        user_id=owner.id,
        legal_hold=held,
        **extra,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TripwireError(Exception):
    """The task went past the hold guard."""


def _past_the_guard(task, *args) -> bool:
    """True when ``task`` went past the hold guard: it hit its first real step, or ran on."""
    try:
        result = task.run(*args)
    except TripwireError:
        return True
    return bool(result.get("status") != SKIPPED_LEGAL_HOLD)


def _trip(monkeypatch, target: str) -> None:
    def _fail(*a, **kw):
        raise TripwireError(target)

    monkeypatch.setattr(target, _fail)


# -------------------------------------------------------------------- the frozen rule itself


def test_held_and_finished_is_frozen_held_and_in_flight_is_not(scoped, normal_user):
    done = _file(scoped, normal_user, held=True)
    running = _file(scoped, normal_user, held=True, status="processing")
    pending = _file(scoped, normal_user, held=True, status="pending")
    free = _file(scoped, normal_user, held=False)

    frozen = frozen_states(scoped, [done.id, running.id, pending.id, free.id])

    assert set(frozen) == {done.id}


# ------------------------------------------------------------- per-file tasks (T1-T3, T7, T8)

_PER_FILE = {
    "summary": (
        "app.tasks.summarization",
        "summarize_transcript_task",
        "app.tasks.summarization._load_summarization_inputs",
    ),
    "topics": (
        "app.tasks.topic_extraction",
        "extract_topics_task",
        "app.tasks.topic_extraction._load_file_context",
    ),
    "speaker_identification": (
        "app.tasks.speaker_identification_task",
        "identify_speakers_llm_task",
        "app.tasks.speaker_identification_task._load_identification_inputs",
    ),
    "rediarize": ("app.tasks.rediarize_task", "rediarize_task", "app.tasks.rediarize_task._start"),
}


@pytest.mark.parametrize("name", sorted(_PER_FILE))
def test_a_task_skips_a_frozen_file(name, scoped, normal_user, monkeypatch):
    import importlib

    module_name, task_name, first_step = _PER_FILE[name]
    task = getattr(importlib.import_module(module_name), task_name)
    _trip(monkeypatch, first_step)
    held = _file(scoped, normal_user, held=True)

    result = task.run(str(held.uuid))

    assert result == {"status": SKIPPED_LEGAL_HOLD, "file_uuid": str(held.uuid)}


@pytest.mark.parametrize("name", sorted(_PER_FILE))
def test_the_same_task_runs_on_a_free_file(name, scoped, normal_user, monkeypatch):
    """Control: on a file nobody holds the task reaches its first real step."""
    import importlib

    module_name, task_name, first_step = _PER_FILE[name]
    task = getattr(importlib.import_module(module_name), task_name)
    _trip(monkeypatch, first_step)
    free = _file(scoped, normal_user, held=False)

    assert _past_the_guard(task, str(free.uuid))


@pytest.mark.parametrize("name", ["summary", "topics", "speaker_identification"])
def test_a_file_held_while_processing_finishes_its_run(name, scoped, normal_user, monkeypatch):
    """A hold placed mid-run preserves the content as of the end of that run (plan §3.1)."""
    import importlib

    module_name, task_name, first_step = _PER_FILE[name]
    task = getattr(importlib.import_module(module_name), task_name)
    _trip(monkeypatch, first_step)
    running = _file(scoped, normal_user, held=True, status="processing")

    assert _past_the_guard(task, str(running.uuid))


def test_a_manual_rediarize_of_a_held_file_is_skipped_even_mid_run(
    scoped, normal_user, monkeypatch
):
    """A re-detection REPLACES speakers: refused whatever the status (the pipeline's own
    post stage, ``pipeline_completion=True``, is the initial run and is not)."""
    from app.tasks import rediarize_task

    _trip(monkeypatch, "app.tasks.rediarize_task._start")
    running = _file(scoped, normal_user, held=True, status="processing")

    result = rediarize_task.rediarize_task.run(str(running.uuid))

    assert result["status"] == SKIPPED_LEGAL_HOLD


def test_speaker_attribute_detection_skips_a_frozen_file(scoped, normal_user, monkeypatch):
    from app.tasks import speaker_attribute_task as sat

    _trip(monkeypatch, "app.tasks.speaker_attribute_task._resolve_file_id_for_tracking")
    held = _file(scoped, normal_user, held=True)

    result = sat.detect_speaker_attributes_task.run(str(held.uuid), normal_user.id)

    assert result["status"] == SKIPPED_LEGAL_HOLD


# ------------------------------------------------------------------- T5 redaction (plan §3.4)


def test_a_redaction_rescan_of_a_frozen_file_is_skipped(scoped, normal_user, monkeypatch):
    from app.tasks import redaction_task

    _trip(monkeypatch, "app.services.redaction.service.RedactionService.detect_and_store")
    held = _file(scoped, normal_user, held=True, redaction_model_version="v1")

    result = redaction_task.redaction_detect_task.run(file_id=held.id)

    assert result == {"status": "skipped", "reason": "legal_hold"}


def test_the_redaction_reindex_sweep_leaves_held_files_out(scoped, normal_user, monkeypatch):
    from app.tasks import redaction_task

    held = _file(scoped, normal_user, held=True)
    free = _file(scoped, normal_user, held=False)
    dispatched: list[int] = []
    monkeypatch.setattr(
        redaction_task.redaction_detect_task,
        "delay",
        lambda file_id, user_id: dispatched.append(file_id),
    )

    redaction_task.redaction_reindex_all_task.run(only_stale=False)

    assert free.id in dispatched
    assert held.id not in dispatched


# ------------------------------------------------------------------------------ T4 auto-label


def test_the_auto_label_backlog_leaves_held_files_out(scoped, normal_user):
    from app.tasks.auto_labeling import pending_suggestion_ids

    held = _file(scoped, normal_user, held=True)
    free = _file(scoped, normal_user, held=False)
    on_held = TopicSuggestion(media_file_id=held.id, user_id=normal_user.id, status="pending")
    on_free = TopicSuggestion(media_file_id=free.id, user_id=normal_user.id, status="pending")
    scoped.add_all([on_held, on_free])
    scoped.commit()

    ids = pending_suggestion_ids(scoped, normal_user.id, [str(held.uuid), str(free.uuid)])

    assert ids == [on_free.id]


def test_auto_apply_writes_nothing_to_a_frozen_file(scoped, normal_user, monkeypatch):
    from app.services.auto_label_service import AutoLabelService

    held = _file(scoped, normal_user, held=True)
    suggestion = TopicSuggestion(
        media_file_id=held.id,
        user_id=normal_user.id,
        status="pending",
        suggested_tags=[{"name": "budget", "confidence": 0.99}],
    )
    scoped.add(suggestion)
    scoped.commit()
    service = AutoLabelService(scoped)
    _trip(monkeypatch, "app.services.auto_label_service.AutoLabelService._add_tag_to_file")

    result = service.auto_apply_suggestions(held, suggestion, normal_user.id)

    assert result["auto_applied_tags"] == []
    scoped.refresh(suggestion)
    assert suggestion.auto_apply_completed_at is None, "left for after the release"


# ---------------------------------------------------------------- T8 cross-file speaker writes


def test_the_attribute_reset_leaves_held_files_speakers_alone(scoped, normal_user):
    from datetime import UTC
    from datetime import datetime

    from app.tasks.speaker_attribute_migration_task import _reset_all_speaker_attributes

    held = _file(scoped, normal_user, held=True)
    free = _file(scoped, normal_user, held=False)
    now = datetime.now(UTC)
    on_held = Speaker(
        user_id=normal_user.id,
        media_file_id=held.id,
        name="SPEAKER_00",
        predicted_gender="female",
        attributes_predicted_at=now,
    )
    on_free = Speaker(
        user_id=normal_user.id,
        media_file_id=free.id,
        name="SPEAKER_00",
        predicted_gender="female",
        attributes_predicted_at=now,
    )
    scoped.add_all([on_held, on_free])
    scoped.commit()

    _reset_all_speaker_attributes(scoped)

    scoped.expire_all()
    assert scoped.get(Speaker, on_held.id).predicted_gender == "female"
    assert scoped.get(Speaker, on_free.id).predicted_gender is None


def test_voice_matching_never_offers_a_held_files_speaker_as_a_candidate(scoped, normal_user):
    from app.api.endpoints.speaker_update import _load_candidates

    held = _file(scoped, normal_user, held=True)
    free = _file(scoped, normal_user, held=False)
    on_held = Speaker(user_id=normal_user.id, media_file_id=held.id, name="SPEAKER_00")
    on_free = Speaker(user_id=normal_user.id, media_file_id=free.id, name="SPEAKER_00")
    scoped.add_all([on_held, on_free])
    scoped.commit()

    candidates = _load_candidates(
        scoped,
        {
            "id": -1,
            "user_id": normal_user.id,
            "organization_id": None,
            "profile_id": None,
            "display_name": "Someone",
        },
    )

    ids = {c["id"] for c in candidates}
    assert on_free.id in ids
    assert on_held.id not in ids
