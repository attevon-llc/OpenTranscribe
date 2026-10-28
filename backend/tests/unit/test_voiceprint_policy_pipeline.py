"""Tiers 1 and 2 of the privacy policy across the voiceprint pipeline stages (issue #1280).

Each stage runs its REAL code against Postgres rows written by the test, with the real policy
reading the user, organization and instance layers. Only the external seams are replaced by
recorders (profile matching, OpenSearch, Celery publishes, the diarizer). The assertion is what
was matched, stored or dispatched, never a mock's call sequence for its own sake.

Every "off" case has an "on" control beside it, in the same parametrization: a gate that
always answered False would pass the off cases and fail the controls.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.models.media import Speaker
from app.models.media import TranscriptSegment

#: How a scope is switched off; "allowed" is the control.
SCOPES = ["allowed", "user", "organization", "instance"]


def _file_in_scope(world, user, scope: str):
    """A media file whose owner/org/instance policy is `scope` (voiceprints only)."""
    org_id = world.new_org().id if scope == "organization" else None
    media_file = world.new_file(user, org_id)
    if scope == "user":
        world.set_user(user, "voiceprints", "false")
    elif scope == "organization":
        world.set_org(org_id, voiceprints=False)
    elif scope == "instance":
        world.set_instance("voiceprints", "false")
    return media_file


def _speakers(db, media_file, labels=("SPEAKER_00", "SPEAKER_01")) -> dict[str, int]:
    rows = [
        Speaker(media_file_id=media_file.id, user_id=media_file.user_id, name=n) for n in labels
    ]
    db.add_all(rows)
    db.flush()
    return {row.name: row.id for row in rows}


@pytest.fixture
def seams(privacy_world, monkeypatch):
    """Recorders for everything a voiceprint stage reaches that is not Postgres or policy."""
    from app.tasks import rediarize_task
    from app.tasks import speaker_embedding_task
    from app.tasks.transcription import embeddings
    from app.tasks.transcription import postprocess

    @contextlib.contextmanager
    def scope():
        yield privacy_world.db

    for module in (embeddings, postprocess, speaker_embedding_task, rediarize_task):
        monkeypatch.setattr(module, "session_scope", scope)
    for module in (embeddings, postprocess):
        monkeypatch.setattr(module, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr("app.utils.task_utils.update_task_status", lambda *a, **k: None)
    monkeypatch.setattr("app.utils.hardware_detection.detect_hardware", MagicMock())

    matching = MagicMock(name="SpeakerMatchingService")
    monkeypatch.setattr(embeddings, "SpeakerMatchingService", matching)
    monkeypatch.setattr("app.services.speaker_matching_service.SpeakerMatchingService", matching)
    monkeypatch.setattr(
        "app.services.permission_service.PermissionService.get_accessible_profile_ids_for_file",
        lambda *a, **k: [],
    )

    staged: list[list[dict]] = []

    def _stage(payload):
        staged.append(payload)
        return {"errors": False}

    monkeypatch.setattr("app.services.opensearch_service.ensure_v4_index_exists", lambda: True)
    monkeypatch.setattr(
        "app.services.opensearch_service.bulk_add_speaker_embeddings_v4",
        _stage,
    )
    monkeypatch.setattr(
        "app.tasks.transcription.embeddings._update_v4_profile_embeddings", lambda *a, **k: 0
    )
    for name in ("send_progress_notification", "send_completion_notification"):
        monkeypatch.setattr(f"app.tasks.transcription.notifications.{name}", lambda *a, **k: None)
    monkeypatch.setattr(postprocess, "send_progress_notification", lambda *a, **k: None)

    published: list[dict] = []

    def _publish(*args, **kwargs):
        published.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(
        "app.tasks.speaker_embedding_task.extract_speaker_embeddings_task.apply_async", _publish
    )
    return type("Seams", (), {"matching": matching, "staged": staged, "published": published})


def _matched(seams) -> int:
    return int(seams.matching.return_value.process_speaker_embeddings_native.call_count)


# ------------------------------------------------------------------ inline matching (P4, P5)


@pytest.mark.parametrize("scope", SCOPES)
def test_the_inline_postprocess_path_matches_speakers_only_when_allowed(
    privacy_world, seams, normal_user, scope
):
    from app.tasks.transcription import postprocess

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    vectors = {label: np.ones(256).tolist() for label in labels}

    postprocess._process_native_embeddings(media_file.id, normal_user.id, "t", vectors, labels)

    assert _matched(seams) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_the_post_stage_inline_path_matches_speakers_only_when_allowed(
    privacy_world, seams, normal_user, scope
):
    from app.tasks.transcription import embeddings
    from app.tasks.transcription.context import TranscriptionContext

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    ctx = TranscriptionContext(
        task_id="t",
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=normal_user.id,
        file_path=media_file.storage_path,
        file_name=media_file.filename,
        content_type=media_file.content_type,
    )

    embeddings._process_speaker_embeddings_native(
        ctx, {label: np.ones(256) for label in labels}, [], labels
    )

    assert _matched(seams) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_native_centroids_are_staged_in_v4_only_when_allowed(
    privacy_world, seams, normal_user, scope
):
    from app.tasks.transcription import postprocess

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    vectors = {label: np.ones(256).tolist() for label in labels}

    postprocess._store_v4_centroids(
        media_file.id, str(media_file.uuid), normal_user.id, vectors, labels
    )

    assert len(seams.staged) == (1 if scope == "allowed" else 0)
    if scope == "allowed":
        assert {row["speaker_id"] for row in seams.staged[0]} == set(labels.values())


@pytest.mark.parametrize("scope", SCOPES)
def test_the_staging_write_itself_refuses_a_forbidden_scope(
    privacy_world, seams, normal_user, scope
):
    """The gate sits in the function every staging caller shares, not in each caller."""
    from app.tasks.transcription import embeddings
    from app.tasks.transcription.context import TranscriptionContext

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    ctx = TranscriptionContext(
        task_id="t",
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=normal_user.id,
        file_path=media_file.storage_path,
        file_name=media_file.filename,
        content_type=media_file.content_type,
    )

    embeddings._store_native_centroids_in_v4_staging(
        ctx, {label: np.ones(256) for label in labels}, labels
    )

    assert len(seams.staged) == (1 if scope == "allowed" else 0)


# ------------------------------------------------------------------ the embedding stage (P4, P6)


@pytest.mark.parametrize("scope", SCOPES)
def test_the_embedding_stage_is_dispatched_only_when_allowed(
    privacy_world, seams, normal_user, scope
):
    from app.tasks.transcription import postprocess

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    gpu_result = {
        "file_id": media_file.id,
        "user_id": normal_user.id,
        "file_uuid": str(media_file.uuid),
        "task_id": "task-1",
    }

    handed_off = postprocess._dispatch_embedding_stage(gpu_result)

    assert handed_off is (scope == "allowed")
    assert len(seams.published) == (1 if scope == "allowed" else 0)


@pytest.fixture
def voiceprint_task(monkeypatch):
    """The voiceprint stage task with its slow phases replaced by recorders."""
    from app.tasks import speaker_embedding_task as mod

    record = SimpleNamespace(extracted=[], completions=[])
    monkeypatch.setattr(
        mod,
        "_extract_aggregated_embeddings",
        lambda *a, **k: record.extracted.append("ran") or {1: np.ones(256)},
    )
    monkeypatch.setattr(mod, "_match_and_finalize_embeddings", lambda *a, **k: 1)
    monkeypatch.setattr(
        mod,
        "_complete_and_meter_parent",
        lambda db, file_id, *a: record.completions.append(file_id),
    )
    for quiet in ("_set_task_progress", "_close_pipeline_task"):
        monkeypatch.setattr(mod, quiet, lambda *a, **k: None)
    monkeypatch.setattr("app.services.minio_service.cleanup_temp_audio", lambda *a, **k: None)
    return record


@pytest.mark.parametrize("scope", SCOPES)
def test_the_voiceprint_stage_task_extracts_nothing_when_policy_forbids_it(
    privacy_world, seams, voiceprint_task, normal_user, scope
):
    from app.tasks import speaker_embedding_task as mod

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    privacy_world.db.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            speaker_id=labels["SPEAKER_00"],
            start_time=0.0,
            end_time=5.0,
            text="one segment",
        )
    )
    privacy_world.db.flush()

    result = mod.extract_speaker_embeddings_task.apply(args=[str(media_file.uuid)]).result

    if scope == "allowed":
        assert result["status"] == "success", result
        assert voiceprint_task.extracted == ["ran"]
    else:
        assert result == {"status": "skipped", "reason": "voiceprints_disabled_by_policy"}
        assert voiceprint_task.extracted == []
        # The stage may be the run's terminus, so the run is still completed and metered.
        assert voiceprint_task.completions == [media_file.id]


# ------------------------------------------------------------------ the post stage (P7)


@pytest.mark.parametrize("scope", SCOPES)
def test_the_post_stage_voiceprint_step_dispatches_only_when_allowed(
    privacy_world, seams, normal_user, scope
):
    from app.tasks import rediarize_task as mod

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    detection = mod._Detection(
        frame=pd.DataFrame(),
        overlap_info={},
        native_embeddings=None,
        provider="native",
        model="m",
    )
    meta = {
        "file_id": media_file.id,
        "user_id": normal_user.id,
        "storage_path": media_file.storage_path,
        "filename": media_file.filename,
        "content_type": media_file.content_type,
    }

    owns_audio = mod._voiceprint_stage(
        meta, str(media_file.uuid), "t", detection, [], labels, "pipeline-1"
    )

    assert owns_audio is (scope == "allowed")
    assert len(seams.published) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_the_post_stage_stages_its_own_centroids_only_when_allowed(
    privacy_world, seams, normal_user, monkeypatch, scope
):
    from app.tasks import rediarize_task as mod

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    # A 256-d centroid does not match an unmigrated 512-d index, so the post stage stages it.
    native = {label: np.ones(256) for label in labels}
    detection = mod._Detection(
        frame=pd.DataFrame(),
        overlap_info={},
        native_embeddings=native,
        provider="native",
        model="m",
    )
    meta = {
        "file_id": media_file.id,
        "user_id": normal_user.id,
        "storage_path": media_file.storage_path,
        "filename": media_file.filename,
        "content_type": media_file.content_type,
    }
    monkeypatch.setattr(
        "app.tasks.transcription.embeddings._should_use_native_embeddings", lambda result: False
    )

    mod._voiceprint_stage(meta, str(media_file.uuid), "t", detection, [], labels, "p")

    assert len(seams.staged) == (1 if scope == "allowed" else 0)


def _run_post_stage(world, seams, user, media_file, monkeypatch):
    """Run the real post stage over one stored segment and return the DiarizeConfig the
    local detector was handed."""
    from app.services.diarization.types import DiarizeResult
    from app.tasks import rediarize_task as mod

    world.db.add(
        TranscriptSegment(
            media_file_id=media_file.id, start_time=0.0, end_time=5.0, text="one segment"
        )
    )
    world.db.flush()
    configs: list = []

    class _Detector:
        def diarize(self, audio_path, config, progress_callback=None):
            configs.append(config)
            frame = pd.DataFrame([{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}])
            return DiarizeResult(
                segments=[],
                num_speakers=1,
                provider_name="native",
                model_name="m",
                metadata={"diarize_df": frame, "overlap_info": {"regions": []}},
            )

    monkeypatch.setattr(mod, "_prepare_audio", lambda *a, **k: ("fake.wav", None))
    monkeypatch.setattr(mod, "_build_detector", lambda *a, **k: _Detector())
    monkeypatch.setattr(mod, "cleanup_temp_audio", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_after_speakers_changed", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.tasks.speaker_attribute_task._is_speaker_attribute_detection_enabled", lambda: False
    )
    monkeypatch.setattr("app.tasks.migration_pipeline.cleanup_gpu_memory", lambda: None)
    result = mod.rediarize_task.apply(
        kwargs={"file_uuid": str(media_file.uuid), "engine": "local", "merge": "segment"}
    ).result
    assert result["status"] == "success", result
    return configs[0]


@pytest.mark.parametrize("scope", SCOPES)
def test_the_post_stage_asks_this_servers_engine_for_centroids_only_when_allowed(
    privacy_world, seams, normal_user, monkeypatch, scope
):
    media_file = _file_in_scope(privacy_world, normal_user, scope)

    config = _run_post_stage(privacy_world, seams, normal_user, media_file, monkeypatch)

    assert config.return_centroids is (scope == "allowed")


@pytest.mark.parametrize("scope", ["allowed", "user", "organization", "instance"])
def test_the_post_stage_asks_for_gender_only_when_gender_is_allowed(
    privacy_world, seams, normal_user, monkeypatch, scope
):
    org_id = privacy_world.new_org().id if scope == "organization" else None
    media_file = privacy_world.new_file(normal_user, org_id)
    if scope == "user":
        privacy_world.set_user(normal_user, "gender", "false")
    elif scope == "organization":
        privacy_world.set_org(org_id, gender=False)
    elif scope == "instance":
        privacy_world.set_instance("gender", "false")

    config = _run_post_stage(privacy_world, seams, normal_user, media_file, monkeypatch)

    assert config.request_gender is (scope == "allowed")
    # Gender is its own switch: turning it off never takes the voiceprints with it.
    assert config.return_centroids is True


# ------------------------------------------------------------------ segment reassignment (P6)


@pytest.mark.parametrize("scope", SCOPES)
def test_a_segment_reassignment_leaves_voiceprints_alone_when_policy_forbids_them(
    privacy_world, seams, normal_user, scope
):
    from app.tasks import speaker_embedding_task as mod

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    labels = _speakers(privacy_world.db, media_file)
    segment = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media_file.id,
        speaker_id=labels["SPEAKER_01"],
        start_time=0.0,
        end_time=5.0,
        text="moved",
    )
    privacy_world.db.add(segment)
    privacy_world.db.flush()
    target = privacy_world.db.get(Speaker, labels["SPEAKER_01"])
    source = privacy_world.db.get(Speaker, labels["SPEAKER_00"])

    plan = mod._load_reassignment_plan(str(segment.uuid), str(target.uuid), str(source.uuid))

    if scope == "allowed":
        assert "skip" not in plan
        assert plan["organization_id"] is None
    else:
        assert plan == {"skip": {"status": "skipped", "reason": "voiceprints_disabled_by_policy"}}


# ------------------------------------------------------------------ the chain (P3)


@pytest.mark.parametrize("scope", SCOPES)
def test_centroids_are_dropped_from_the_chain_when_policy_forbids_them(
    privacy_world, normal_user, scope
):
    from app.tasks.transcription.finalize import _centroids_for_chain

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    result = {"native_speaker_embeddings": {"SPEAKER_00": np.ones(256)}}

    serialized, use_native = _centroids_for_chain(result, media_file.id)

    if scope == "allowed":
        assert serialized is not None
        assert len(serialized["SPEAKER_00"]) == 256
    else:
        assert (serialized, use_native) == (None, False)


def test_no_centroids_in_the_result_means_nothing_to_carry(privacy_world, normal_user):
    from app.tasks.transcription.finalize import _centroids_for_chain

    media_file = _file_in_scope(privacy_world, normal_user, "allowed")

    assert _centroids_for_chain({}, media_file.id) == (None, False)


# ------------------------------------------------------------------ clustering dispatch (P4)


@pytest.mark.parametrize("scope", SCOPES)
def test_speaker_clustering_is_dispatched_only_when_voiceprints_are_allowed(
    privacy_world, seams, normal_user, monkeypatch, scope
):
    from app.core.enums import PostProcessingState
    from app.tasks.transcription import postprocess

    media_file = _file_in_scope(privacy_world, normal_user, scope)
    queued: list[tuple] = []
    monkeypatch.setattr(
        "app.tasks.speaker_clustering.cluster_speakers_for_file.delay",
        lambda *a, **k: queued.append(a),
    )

    state = postprocess._dispatch_speaker_clustering(
        str(media_file.uuid), normal_user.id, None, file_id=media_file.id
    )

    if scope == "allowed":
        assert state == PostProcessingState.QUEUED
        assert queued == [(str(media_file.uuid), normal_user.id)]
    else:
        assert state == PostProcessingState.SKIPPED
        assert queued == []


# ------------------------------------------------------------------ dispatch plans per file (P1)


@pytest.mark.parametrize("scope", SCOPES)
def test_dispatch_plans_the_voiceprint_step_from_the_files_own_policy(
    privacy_world, normal_user, scope
):
    from app.tasks.transcription.dispatch import _plan_for_dispatch

    media_file = _file_in_scope(privacy_world, normal_user, scope)

    plan = _plan_for_dispatch(
        privacy_world.db, media_file, whisper_model=None, skip_diarization=False
    )

    # Local Whisper detecting speakers inline hands back centroids: the one cell the policy
    # has to take the voiceprint step out of.
    assert plan.diarization.execution == "inline_local"
    assert plan.speaker_id.execution == ("inline_native" if scope == "allowed" else "none")
    issues = [i.code for i in plan.issues]
    assert ("voiceprints_disabled" in issues) is (scope != "allowed")


def test_dispatch_judges_the_files_organization_not_the_dispatchers(privacy_world, normal_user):
    """An org that forbids voiceprints governs its file even when the owner's own switch is on,
    and an unrelated org's switch governs nothing."""
    from app.tasks.transcription.dispatch import _plan_for_dispatch

    forbidding, other = privacy_world.new_org(), privacy_world.new_org()
    privacy_world.set_org(forbidding.id, voiceprints=False)
    in_forbidding = privacy_world.new_file(normal_user, forbidding.id)
    in_other = privacy_world.new_file(normal_user, other.id)

    def execution(media_file):
        plan = _plan_for_dispatch(
            privacy_world.db, media_file, whisper_model=None, skip_diarization=False
        )
        return plan.speaker_id.execution

    assert execution(in_forbidding) == "none"
    assert execution(in_other) == "inline_native"
