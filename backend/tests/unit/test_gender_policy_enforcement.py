"""Gender inference is one switch with a ceiling above the user (issue #1280, plan test 8).

Every producer and consumer of inferred gender in the WP4 file set is run against a real
session with the organization switched OFF and the user's own switch explicitly ON, which is
the case the old resolver got wrong ("user > system > default" let the user overrule everyone
above them). Each "off" case has an "on" control beside it, because a gate that always says no
would pass the off cases.

State is set straight into ``organization_policy`` / ``system_settings`` / ``user_setting``.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_mod
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core import constants
from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.models.system_settings import SystemSettings
from app.services import post_processing_status
from app.services.speaker_attribute_settings import resolve_speaker_attribute_flags
from app.services.speaker_attribute_settings import resolve_speaker_attribute_flags_for_file
from app.tasks import combined_speaker_analysis_task as combined
from app.tasks import speaker_attribute_migration_task as migration
from app.tasks import speaker_attribute_task as sat
from app.tasks import speaker_identification_task as sid
from app.tasks.transcription.speaker_processor import apply_sidecar_gender_for_user

SIDECAR = {"SPEAKER_00": {"label": "female", "confidence": 0.9}}
USER_KEY = constants.PRIVACY_USER_GENDER_DETECTION_KEY


@contextlib.contextmanager
def _real_scope(db_session):
    try:
        yield db_session
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def _org(db, *, gender: bool = True) -> Organization:
    org = Organization(name=f"org-{uuid_mod.uuid4().hex[:8]}")
    db.add(org)
    db.flush()
    db.add(OrganizationPolicy(organization_id=org.id, gender_inference_enabled=gender))
    db.commit()
    return org


def _set_org_gender(db, org_id: int, value: bool) -> None:
    db.query(OrganizationPolicy).filter(OrganizationPolicy.organization_id == org_id).update(
        {OrganizationPolicy.gender_inference_enabled: value}
    )
    db.commit()


def _user_on(db, user) -> None:
    """The user explicitly switched gender ON: the thing an organization must still override."""
    db.add(UserSetting(user_id=user.id, setting_key=USER_KEY, setting_value="true"))
    db.commit()


def _file(db, user, org: Organization | None, **speaker_kw: Any):
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        organization_id=org.id if org else None,
        filename="a.mp4",
        storage_path="test/a.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
    )
    db.add(media_file)
    db.flush()
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user.id,
        name="SPEAKER_00",
        **speaker_kw,
    )
    db.add(speaker)
    db.flush()
    for j in range(2):
        db.add(
            TranscriptSegment(
                uuid=str(uuid_mod.uuid4()),
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=j * 4.0,
                end_time=j * 4.0 + 4.0,
                text="hello there",
            )
        )
    db.commit()
    return media_file, [speaker]


def _prepared(media_file: MediaFile, user) -> migration.PreparedFile:
    return migration.PreparedFile(
        file_uuid=str(media_file.uuid),
        audio_source="https://example/presigned",
        speakers=[],
        speaker_segments={},
        media_file_id=media_file.id,
        user_id=user.id,
    )


def _gender_results(speaker: Speaker) -> dict[str, list[Any]]:
    from app.services.speaker_analysis_models import SegmentResult

    return {
        "gender": [
            SegmentResult(
                speaker_id=speaker.id,
                model_name="gender",
                value=("female", 0.8),
            )
        ]
    }


# ---------------------------------------------------------------- the resolver


def test_an_organization_that_is_off_overrides_a_user_who_is_on(db_session, normal_user):
    org = _org(db_session, gender=False)
    _user_on(db_session, normal_user)

    flags = resolve_speaker_attribute_flags(db_session, normal_user.id, organization_id=org.id)

    assert not flags.gender_detection_enabled
    assert not flags.gender_prediction_allowed
    assert flags.gender_locked_by == "organization"


def test_the_same_user_is_unrestricted_in_an_organization_that_is_on(db_session, normal_user):
    org = _org(db_session, gender=True)
    _user_on(db_session, normal_user)

    flags = resolve_speaker_attribute_flags(db_session, normal_user.id, organization_id=org.id)

    assert flags.gender_detection_enabled and flags.gender_prediction_allowed
    assert flags.gender_locked_by is None


def test_the_users_own_switch_is_not_reported_as_a_lock(db_session, normal_user):
    db_session.add(UserSetting(user_id=normal_user.id, setting_key=USER_KEY, setting_value="false"))
    db_session.commit()

    flags = resolve_speaker_attribute_flags(db_session, normal_user.id)

    assert not flags.gender_detection_enabled
    assert flags.gender_locked_by is None  # they can undo it, so it is not a lock


def test_the_instance_switch_is_a_ceiling_not_a_default(db_session, normal_user):
    """``speaker_attribute.*`` is a default a user may override; ``privacy.*`` is a ceiling."""
    _user_on(db_session, normal_user)
    db_session.add(SystemSettings(key=constants.PRIVACY_GENDER_INFERENCE_KEY, value="false"))
    db_session.commit()

    flags = resolve_speaker_attribute_flags(db_session, normal_user.id)

    assert not flags.gender_detection_enabled
    assert flags.gender_locked_by == "instance"


def test_the_organization_comes_from_the_files_row_not_from_the_caller(db_session, normal_user):
    org = _org(db_session, gender=False)
    media_file, _ = _file(db_session, normal_user, org)
    _user_on(db_session, normal_user)

    by_file = resolve_speaker_attribute_flags_for_file(db_session, media_file.id)
    personal_scope = resolve_speaker_attribute_flags(db_session, normal_user.id)

    assert not by_file.gender_detection_enabled and by_file.gender_locked_by == "organization"
    # A caller that forgets the organization is in personal scope and sees no organization
    # row: this is why every file-bound reader goes through the _for_file form.
    assert personal_scope.gender_detection_enabled


def test_a_file_that_cannot_be_read_denies_gender(db_session, normal_user):
    flags = resolve_speaker_attribute_flags_for_file(db_session, 2**31 - 1, user_id=normal_user.id)

    assert not flags.gender_detection_enabled and not flags.gender_prediction_allowed


# ---------------------------------------------------------------- G3: the sidecar write


def test_sidecar_verdicts_are_ignored_when_the_org_is_off_and_the_user_is_on(
    db_session, normal_user
):
    org = _org(db_session, gender=False)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    _user_on(db_session, normal_user)

    stored = apply_sidecar_gender_for_user(db_session, media_file.id, normal_user.id, SIDECAR)
    db_session.commit()

    assert stored == 0
    db_session.refresh(speaker)
    assert speaker.predicted_gender is None
    assert speaker.attribute_confidence is None
    assert speaker.attributes_predicted_at is None  # a later opt-in must still find it pending


def test_sidecar_verdicts_are_stored_when_the_org_is_on(db_session, normal_user):
    org = _org(db_session, gender=True)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    _user_on(db_session, normal_user)

    stored = apply_sidecar_gender_for_user(db_session, media_file.id, normal_user.id, SIDECAR)
    db_session.commit()

    assert stored == 1
    db_session.refresh(speaker)
    assert speaker.predicted_gender == "female"


@pytest.mark.parametrize(
    "verdict",
    [{"label": None, "confidence": None}, {"label": "female", "confidence": None}, {}],
    ids=["null-label-and-confidence", "label-without-confidence", "empty"],
)
def test_an_incomplete_verdict_is_still_null_safe_with_the_policy_on(
    db_session, normal_user, verdict
):
    """The path that once crashed every transcription must survive the new gate."""
    org = _org(db_session, gender=True)
    media_file, (speaker,) = _file(db_session, normal_user, org)

    stored = apply_sidecar_gender_for_user(
        db_session, media_file.id, normal_user.id, {"SPEAKER_00": verdict}
    )

    assert stored == 0
    db_session.refresh(speaker)
    assert speaker.predicted_gender is None


# ---------------------------------------------------------------- G2: the attribute task


@pytest.fixture
def reports(monkeypatch):
    """Capture what the post-processing card is told, for the task under test."""
    seen: list[tuple[PostProcessingTask, PostProcessingState]] = []

    def fake_report(task, state, **_kw):
        seen.append((task, state))
        return True

    monkeypatch.setattr(post_processing_status, "report", fake_report)
    monkeypatch.setattr(
        post_processing_status,
        "resolve_file",
        lambda **kw: post_processing_status.FileRef(id=1, uuid="u", owner_id=1),
    )
    return seen


@pytest.fixture
def task_env(db_session, monkeypatch):
    monkeypatch.setattr(sat, "session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr(sat, "_dispatch_llm_speaker_identification", lambda file_uuid: None)
    monkeypatch.setattr(sat, "send_ws_event_for_file", lambda *a, **kw: None)
    monkeypatch.setattr(
        "app.services.takedown_service.is_notification_suppressed_for_uuid",
        lambda *a, **kw: False,
    )
    minio = MagicMock()
    minio.presigned_get_object.return_value = "http://minio.invalid/a.mp4"
    monkeypatch.setattr("app.services.minio_service.minio_client", minio)
    monkeypatch.setattr(
        "app.services.speaker_attribute_service.get_cached_attribute_service",
        lambda: MagicMock(),
    )
    state: dict[str, Any] = {"inference": 0, "during_inference": None}

    def fake_inference(audio_source, work_items, service):
        state["inference"] += 1
        if state["during_inference"] is not None:
            state["during_inference"]()
        return {work_items[0][0]: {"male": 0.9, "female": 0.1}}, {work_items[0][0]: 1}

    monkeypatch.setattr(sat, "_run_gender_inference_parallel", fake_inference)
    return state


def _run_task(media_file, user):
    return sat.detect_speaker_attributes_task.apply(args=[str(media_file.uuid), user.id]).get()


def _last_state(seen, task=PostProcessingTask.SPEAKER_ATTRIBUTES):
    return [state for t, state in seen if t == task][-1]


def test_the_attribute_task_is_skipped_by_policy_and_reports_skipped(
    db_session, normal_user, task_env, reports
):
    org = _org(db_session, gender=False)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    _user_on(db_session, normal_user)

    result = _run_task(media_file, normal_user)

    assert result == {"status": "skipped", "reason": "gender_policy_disabled"}
    assert task_env["inference"] == 0  # no model ran
    db_session.refresh(speaker)
    assert speaker.predicted_gender is None and speaker.attributes_predicted_at is None
    # The card must be told, or it waits forever (#1313).
    assert _last_state(reports) is PostProcessingState.SKIPPED


def test_the_attribute_task_still_predicts_and_reports_done_when_the_org_is_on(
    db_session, normal_user, task_env, reports
):
    org = _org(db_session, gender=True)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    _user_on(db_session, normal_user)

    result = _run_task(media_file, normal_user)

    assert result["status"] == "success", result
    assert task_env["inference"] == 1
    db_session.refresh(speaker)
    assert speaker.predicted_gender == "male"
    assert _last_state(reports) is PostProcessingState.DONE


def test_a_switch_flipped_during_inference_drops_the_verdicts_and_reports_skipped(
    db_session, normal_user, task_env, reports
):
    """Inference takes minutes; the write re-reads the policy instead of trusting the gate."""
    org = _org(db_session, gender=True)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    _user_on(db_session, normal_user)
    task_env["during_inference"] = lambda: _set_org_gender(db_session, org.id, False)

    result = _run_task(media_file, normal_user)

    assert task_env["inference"] == 1  # it did start, so the early gate passed
    assert result == {"status": "skipped", "reason": "gender_policy_disabled"}
    db_session.refresh(speaker)
    assert speaker.predicted_gender is None
    assert speaker.attributes_predicted_at is None
    assert _last_state(reports) is PostProcessingState.SKIPPED


# ---------------------------------------------------------------- G5: the admin batch writer


def test_the_migration_writer_leaves_a_forbidden_speaker_untouched(db_session, normal_user):
    org = _org(db_session, gender=False)
    stamped = datetime(2026, 1, 1, tzinfo=UTC)
    media_file, (speaker,) = _file(
        db_session,
        normal_user,
        org,
        predicted_gender="male",
        attribute_confidence={"gender": 0.7},
        attributes_predicted_at=stamped,
    )
    migration_session = _real_scope(db_session)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(migration, "session_scope", lambda: migration_session)
        written = migration._gender_result_writer(
            _prepared(media_file, normal_user), _gender_results(speaker)
        )
    db_session.commit()

    assert written == 0
    db_session.refresh(speaker)
    assert speaker.predicted_gender == "male"  # hidden, not overwritten
    assert speaker.attribute_confidence == {"gender": 0.7}
    assert speaker.attributes_predicted_at == stamped


def test_the_migration_writer_writes_for_an_allowed_speaker(db_session, normal_user, monkeypatch):
    org = _org(db_session, gender=True)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    monkeypatch.setattr(migration, "session_scope", lambda: _real_scope(db_session))

    written = migration._gender_result_writer(
        _prepared(media_file, normal_user), _gender_results(speaker)
    )

    assert written == 1
    db_session.refresh(speaker)
    assert speaker.predicted_gender == "female"
    assert speaker.attributes_predicted_at is not None


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    def ping(self):
        return True


def _run_orchestrator(db_session, monkeypatch, *, force: bool, user) -> tuple[dict, list]:
    dispatched: list[list[str]] = []
    monkeypatch.setattr(migration.attribute_migration_progress, "_redis_client", _FakeRedis())
    monkeypatch.setattr(migration, "session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr(migration, "send_ws_event", lambda *a, **kw: None)

    def fake_apply_async(kwargs, priority):
        dispatched.append(list(kwargs["file_uuids"]))
        return SimpleNamespace(id=f"batch-{len(dispatched)}")

    monkeypatch.setattr(
        migration.detect_speaker_attributes_batch_task, "apply_async", fake_apply_async
    )
    migration.migrate_speaker_attributes_task.push_request(id="orchestrator-test")
    try:
        result = migration.migrate_speaker_attributes_task.run(user_id=user.id, force=force)
    finally:
        migration.migrate_speaker_attributes_task.pop_request()
    return result, dispatched


def test_force_migration_resets_and_dispatches_only_files_where_gender_is_allowed(
    db_session, normal_user, monkeypatch
):
    stamped = datetime(2026, 1, 1, tzinfo=UTC)
    kept = {
        "predicted_gender": "male",
        "attribute_confidence": {"gender": 0.7},
        "attributes_predicted_at": stamped,
    }
    off_org = _org(db_session, gender=False)
    on_org = _org(db_session, gender=True)
    off_file, (off_speaker,) = _file(db_session, normal_user, off_org, **kept)
    on_file, (on_speaker,) = _file(db_session, normal_user, on_org, **kept)

    result, dispatched = _run_orchestrator(db_session, monkeypatch, force=True, user=normal_user)

    assert result["status"] == "started", result
    all_dispatched = {u for batch in dispatched for u in batch}
    assert str(on_file.uuid) in all_dispatched
    assert str(off_file.uuid) not in all_dispatched
    assert result["skipped_by_policy"] >= 1
    db_session.expire_all()
    # The forbidden file keeps its stored verdict: switching off hides, only a purge deletes.
    assert db_session.get(Speaker, off_speaker.id).predicted_gender == "male"
    assert db_session.get(Speaker, off_speaker.id).attributes_predicted_at == stamped
    # The control: an allowed file is reset for re-detection exactly as before.
    assert db_session.get(Speaker, on_speaker.id).predicted_gender is None
    assert db_session.get(Speaker, on_speaker.id).attributes_predicted_at is None


def test_a_batch_that_waited_in_the_queue_skips_a_file_forbidden_since(
    db_session, normal_user, monkeypatch
):
    org = _org(db_session, gender=False)
    media_file, _ = _file(db_session, normal_user, org)
    monkeypatch.setattr(migration, "session_scope", lambda: _real_scope(db_session))
    progress = MagicMock()
    progress.is_running.return_value = True
    monkeypatch.setattr(migration, "attribute_migration_progress", progress)
    monkeypatch.setattr(migration, "ProgressTracker", MagicMock())
    monkeypatch.setattr(migration, "_emit_attr_progress", lambda *a, **kw: None)
    prepared = MagicMock(side_effect=AssertionError("a forbidden file must not be prepared"))
    monkeypatch.setattr("app.tasks.migration_pipeline.prepare_file", prepared)

    result = migration.detect_speaker_attributes_batch_task.run([str(media_file.uuid)])

    assert result["status"] == "empty"
    assert result["skipped_by_policy"] == 1
    prepared.assert_not_called()
    # Counted as done, not failed: a policy skip must not look like an error to the admin.
    progress.increment_processed.assert_called_once_with(success=True)


# ---------------------------------------------------------------- G5: the combined writer


def test_the_combined_writer_still_writes_embeddings_but_not_forbidden_gender(
    db_session, normal_user, monkeypatch
):
    org = _org(db_session, gender=False)
    media_file, (speaker,) = _file(db_session, normal_user, org)
    monkeypatch.setattr(migration, "session_scope", lambda: _real_scope(db_session))
    embedding_calls: list[str] = []

    def fake_embedding_writer(prepared, results):
        embedding_calls.append(prepared.file_uuid)
        return 3

    monkeypatch.setattr(
        "app.tasks.embedding_migration_v4._embedding_result_writer", fake_embedding_writer
    )

    written = combined._combined_result_writer(
        _prepared(media_file, normal_user), _gender_results(speaker)
    )

    assert embedding_calls == [str(media_file.uuid)]  # the other half is not collateral damage
    assert written == 3  # gender contributed nothing
    db_session.refresh(speaker)
    assert speaker.predicted_gender is None and speaker.attributes_predicted_at is None


# ---------------------------------------------------------------- G6: the speaker-ID LLM


def _speaker_with_voice_verdict(db_session, normal_user, org):
    media_file, (speaker,) = _file(
        db_session,
        normal_user,
        org,
        predicted_gender="female",
        attribute_confidence={"gender": 0.92},
    )
    return media_file, speaker


@pytest.fixture
def one_cross_reference(monkeypatch):
    """Pin the (library-dependent) name guess so the test owns the cross-reference."""
    refs = [
        {
            "hint_name": "Jane Doe",
            "hint_role": "host",
            "speaker_label": "SPEAKER_00",
            "name_gender": "female",
            "voice_gender": "female",
            "voice_confidence": 0.92,
            "alignment": "match",
            "confidence_boost": 0.1,
        }
    ]
    monkeypatch.setattr(sid, "cross_reference_attributes", lambda *a, **kw: refs)
    monkeypatch.setattr(
        sid.MetadataSpeakerExtractor,
        "extract",
        lambda self, metadata: SimpleNamespace(hints=[]),
    )


def _cross_reference(db_session, media_file) -> str:
    return sid._gender_cross_reference_context(
        db_session, int(media_file.id), media_file, speaker_segments=[]
    )


def test_cross_reference_is_built_and_alignment_stored_when_gender_is_allowed(
    db_session, normal_user, one_cross_reference
):
    org = _org(db_session, gender=True)
    media_file, speaker = _speaker_with_voice_verdict(db_session, normal_user, org)

    block = _cross_reference(db_session, media_file)

    assert "voice: female 92%" in block
    db_session.refresh(speaker)
    assert speaker.attribute_confidence["alignment"] == "match"


def test_cross_reference_is_not_derived_at_all_when_gender_is_off(
    db_session, normal_user, one_cross_reference
):
    org = _org(db_session, gender=False)
    media_file, speaker = _speaker_with_voice_verdict(db_session, normal_user, org)

    block = _cross_reference(db_session, media_file)

    assert block == ""
    db_session.refresh(speaker)
    assert "alignment" not in speaker.attribute_confidence  # nothing derived from the verdict
    assert speaker.predicted_gender == "female"  # and nothing deleted either


LOCAL = SimpleNamespace(provider="vllm", base_url="http://192.168.1.20:8000/v1")
REMOTE = SimpleNamespace(provider="openai", base_url="https://api.openai.com/v1")
BLOCK = "SPEAKER ATTRIBUTE CROSS-REFERENCES:\n  voice: female 92%"


def test_a_remote_provider_never_receives_the_gender_cross_reference():
    context = sid._with_gender_context("TITLE: Interview", BLOCK, REMOTE)

    assert context == "TITLE: Interview"
    assert "female" not in context


def test_a_local_provider_does_receive_it():
    """The control for the test above: the block is wanted when the text stays on the machine."""
    context = sid._with_gender_context("TITLE: Interview", BLOCK, LOCAL)

    assert BLOCK in context and context.startswith("TITLE: Interview")


def test_an_unclassifiable_provider_reads_as_remote():
    assert sid._with_gender_context("TITLE", BLOCK, None) == "TITLE"
    assert sid._with_gender_context("TITLE", BLOCK, SimpleNamespace(provider="vllm")) == "TITLE"


@pytest.mark.parametrize(("config", "expect_gender"), [(REMOTE, False), (LOCAL, True)])
def test_the_prompt_the_llm_receives_follows_the_provider(monkeypatch, config, expect_gender):
    sent: dict[str, str] = {}
    service = MagicMock()
    service.config = config
    monkeypatch.setattr(sid, "_create_llm_service", lambda user_id: service)
    monkeypatch.setattr(sid, "llm_facts", lambda cfg: {})

    def fake_run(llm_service, transcript, segments, known, language, metadata_context):
        sent["context"] = metadata_context
        return {"speaker_predictions": []}

    monkeypatch.setattr(sid, "_run_llm_identification", fake_run)

    sid._generate_predictions(1, 1, "t", [], [], "TITLE: Interview", "en", gender_context=BLOCK)

    assert ("voice: female 92%" in sent["context"]) is expect_gender
    assert sent["context"].startswith("TITLE: Interview")
