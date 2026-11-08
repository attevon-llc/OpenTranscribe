# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site: a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""Speaker-audio and cloud-diarization tasks hold no DB transaction across their slow phase.

See ``tests/unit/_session_lifetime_trackers.py`` for the depth-tracking scope stand-in
and the measured incident behind this family of tests.
"""

import uuid as uuid_mod

import numpy as np
import pytest

from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.tasks import speaker_embedding_reassignment_task as rsb
from app.tasks import speaker_embedding_task as seb
from app.tasks.transcription import embeddings as temb
from tests.unit._session_lifetime_trackers import ScopeTracker
from tests.unit._session_lifetime_trackers import leak
from tests.unit._session_lifetime_trackers import make_transcribed_file


# --------------------------------------------------------------------------- #
# 1. extract_speaker_embeddings (GPU queue) — the same table as the known leak
# --------------------------------------------------------------------------- #
class _FakeHardware:
    def __init__(self, tracker):
        self._tracker = tracker

    def optimize_memory_usage(self):
        self._tracker.observe("gpu_sync")


class _FakeEmbeddingService:
    mode = "test"
    model_name = "fake-embed"

    def __init__(self, tracker):
        self._tracker = tracker

    def extract_embeddings_for_segments(self, audio_path, segments, speaker_mapping):
        # The real implementation runs up to 5 ffmpeg slices per speaker and an
        # inference pass on each.
        self._tracker.observe("extract_embeddings")
        assert segments, "fixture produced no segments — the test would prove nothing"
        assert speaker_mapping, "fixture produced no speaker mapping"
        return {sid: [np.array([3.0, 4.0, 0.0])] for sid in speaker_mapping.values()}

    def aggregate_embeddings(self, embeddings):
        stacked = np.vstack(embeddings)
        vec = np.mean(stacked, axis=0)
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def get_embedding_dimension(self):
        return 3

    def cleanup(self):
        self._tracker.observe("model_cleanup")


@pytest.fixture
def embedding_task_env(db_session, monkeypatch):
    """Patch ``speaker_embedding_task``'s slow seams so they are observable and instant."""
    tracker = ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(seb, "session_scope", tracker.scope)
    # The privacy gate reads the file's policy in its own short scope, which the tracker also
    # counts: it must close before the slow phase starts like every other scope.
    monkeypatch.setattr("app.services.privacy.gates.session_scope", tracker.scope)

    # --- audio staging (MinIO) ---
    def _temp_exists(file_uuid):
        tracker.observe("temp_audio_exists")
        return True

    monkeypatch.setattr("app.services.temp_audio_probe.temp_audio_exists", _temp_exists)
    monkeypatch.setattr(
        "app.services.minio_service.download_temp_audio",
        lambda file_uuid, path: tracker.observe("download_temp_audio"),
    )
    monkeypatch.setattr("app.services.minio_service.cleanup_temp_audio", lambda file_uuid: None)

    # --- GPU + model ---
    monkeypatch.setattr(
        "app.utils.hardware_detection.detect_hardware", lambda: _FakeHardware(tracker)
    )
    monkeypatch.setattr(
        "app.services.speaker_embedding_service.SpeakerEmbeddingService",
        lambda *a, **kw: _FakeEmbeddingService(tracker),
    )

    # --- profile matching (legitimately needs a session) ---
    class _FakeMatching:
        def __init__(self, db, embedding_service=None):
            self.db = db
            self.embedding_service = embedding_service
            recorded["embedding_service"] = embedding_service

        def process_speaker_embeddings_native(
            self, *, media_file_id, user_id, native_embeddings, accessible_profile_ids=None
        ):
            tracker.observe("speaker_matching")
            recorded["native_embeddings"] = native_embeddings
            return [{"speaker_id": sid} for sid in native_embeddings]

        def process_speaker_segments(
            self,
            audio_path,
            media_file_id,
            user_id,
            segments,
            speaker_mapping,
            accessible_profile_ids=None,
        ):
            # Not called by the current code. Kept so that reverting the fix
            # (which called this instead) fails on the scope-depth assertion —
            # the behaviour under test — rather than on an AttributeError.
            raw = self.embedding_service.extract_embeddings_for_segments(
                audio_path, segments, speaker_mapping
            )
            tracker.observe("speaker_matching")
            return [{"speaker_id": sid} for sid in raw]

    monkeypatch.setattr(
        "app.services.speaker_matching_service.SpeakerMatchingService", _FakeMatching
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_completion_notification",
        lambda user_id, file_id: None,
    )

    tracker.recorded = recorded
    return tracker


def test_extract_speaker_embeddings_holds_no_transaction_across_audio_and_model(
    db_session, normal_user, embedding_task_env
):
    """The regression: download, ffmpeg and inference must run with zero scopes open."""
    tracker = embedding_task_env
    media_file, speakers = make_transcribed_file(db_session, normal_user)
    speaker_mapping = {s.name: s.id for s in speakers}

    async_result = seb.extract_speaker_embeddings_task.apply(args=[str(media_file.uuid)])
    result = async_result.get()

    assert result["status"] == "success", result

    observed = tracker.seen
    for label in (
        "temp_audio_exists",
        "download_temp_audio",
        "extract_embeddings",
        "model_cleanup",
    ):
        assert observed[label] == 0, leak(tracker, label)

    # Profile matching is pure DB/OpenSearch work — it is *supposed* to be
    # inside a scope. Asserting that pins the split rather than letting a task
    # that stopped using the DB at all pass the checks above.
    assert observed["speaker_matching"] == 1, tracker.observations
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"

    # The embeddings that reach matching are the aggregated, L2-normalized
    # vectors the pre-split code matched on.
    native = tracker.recorded["native_embeddings"]
    assert set(native) == set(speaker_mapping.values())
    assert np.allclose(next(iter(native.values())), np.array([0.6, 0.8, 0.0]))
    assert tracker.recorded["embedding_service"] is None

    task_row = db_session.query(TaskModel).filter(TaskModel.id == async_result.task_id).first()
    assert task_row is not None and task_row.status == "completed"
    assert result["speakers_processed"] == len(speakers)


def test_extract_speaker_embeddings_releases_scope_when_inference_fails(
    db_session, normal_user, embedding_task_env, monkeypatch
):
    """A failure in the slow phase must not leave a scope open either."""
    tracker = embedding_task_env
    media_file, speakers = make_transcribed_file(db_session, normal_user)

    def boom(self, audio_path, segments, speaker_mapping):
        tracker.observe("extract_embeddings")
        raise RuntimeError("ffmpeg segment fetch failed")

    monkeypatch.setattr(_FakeEmbeddingService, "extract_embeddings_for_segments", boom)

    result = seb.extract_speaker_embeddings_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "error"
    assert tracker.seen["extract_embeddings"] == 0, leak(tracker, "extract_embeddings")
    assert tracker.depth == 0, "a session scope survived the failure"


def test_speaker_embedding_read_phase_returns_plain_data(
    db_session, normal_user, embedding_task_env
):
    """``_load_speaker_embedding_inputs`` must not hand back live ORM instances.

    Returning ORM objects would push lazy loads — and therefore a new
    transaction — into the slow phase, reintroducing the leak by the back door.
    """
    tracker = embedding_task_env
    media_file, speakers = make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = seb._load_speaker_embedding_inputs(str(media_file.uuid), str(uuid_mod.uuid4()))

    assert tracker.depth == 0
    assert inputs["file_id"] == media_file.id
    assert inputs["user_id"] == normal_user.id
    assert inputs["storage_path"] == media_file.storage_path
    assert len(inputs["processed_segments"]) == 2
    assert inputs["processed_segments"][0] == {
        "start": 0.0,
        "end": 4.0,
        "text": "hello there",
        "speaker": speakers[0].name,
        "speaker_id": speakers[0].id,
    }
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))
    for segment in inputs["processed_segments"]:
        for field in segment.values():
            assert not isinstance(field, (MediaFile, Speaker, TranscriptSegment))


# --------------------------------------------------------------------------- #
# 1b. update_speaker_embedding_on_reassignment (GPU queue, interactive)
# --------------------------------------------------------------------------- #
@pytest.fixture
def reassignment_env(db_session, monkeypatch):
    tracker = ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(rsb, "session_scope", tracker.scope)

    def _download(storage_path):
        tracker.observe("minio_download")
        import io

        return io.BytesIO(b"fake-media"), "video/mp4", 10

    monkeypatch.setattr("app.services.minio_service.download_file", _download)

    def _prepare(
        temp_file_path, content_type, temp_dir, progress_callback=None, *, audio_stream_index=None
    ):
        tracker.observe("ffmpeg_prepare")
        return temp_file_path

    monkeypatch.setattr(
        "app.tasks.transcription.audio_processor.prepare_audio_for_transcription", _prepare
    )

    class _FakeCached:
        def extract_embedding_from_file(self, path, segment):
            tracker.observe("segment_inference")
            return np.array([0.0, 1.0, 0.0])

    monkeypatch.setattr(
        "app.services.speaker_embedding_service.get_cached_embedding_service",
        lambda: _FakeCached(),
    )

    def _get_doc(uuid):
        tracker.observe("opensearch_get")
        return None

    monkeypatch.setattr("app.services.opensearch_service.get_speaker_document", _get_doc)

    def _add(**kwargs):
        tracker.observe("opensearch_write")
        recorded["add_speaker_embedding"] = kwargs

    monkeypatch.setattr("app.services.opensearch_service.add_speaker_embedding", _add)
    monkeypatch.setattr(
        "app.services.opensearch_service.update_speaker_segment_count",
        lambda uuid, count: tracker.observe("opensearch_count"),
    )

    tracker.recorded = recorded
    return tracker


def test_reassignment_holds_no_transaction_across_download_and_inference(
    db_session, normal_user, reassignment_env
):
    tracker = reassignment_env
    media_file, speakers = make_transcribed_file(db_session, normal_user)
    segment = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id == speakers[1].id)
        .first()
    )

    result = rsb.update_speaker_embedding_on_reassignment.apply(
        args=[
            str(segment.uuid),
            str(media_file.uuid),
            str(speakers[1].uuid),
            str(speakers[0].uuid),
            normal_user.id,
        ]
    ).get()

    assert result["status"] == "success", result

    observed = tracker.seen
    for label in ("minio_download", "ffmpeg_prepare", "segment_inference"):
        assert observed[label] == 0, leak(tracker, label)
    # The OpenSearch phase needs no DB session at all.
    for label in ("opensearch_get", "opensearch_write"):
        assert observed[label] == 0, leak(tracker, label)

    assert tracker.opened >= 1
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    stored = tracker.recorded["add_speaker_embedding"]
    assert stored["speaker_uuid"] == str(speakers[1].uuid)
    assert stored["speaker_id"] == speakers[1].id
    assert stored["segment_count"] == 1


def test_reassignment_read_phase_returns_plain_data(db_session, normal_user, reassignment_env):
    tracker = reassignment_env
    _, speakers = make_transcribed_file(db_session, normal_user)
    segment = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id == speakers[0].id)
        .first()
    )

    plan = rsb._load_reassignment_plan(
        str(segment.uuid), str(speakers[0].uuid), str(speakers[1].uuid)
    )

    assert tracker.depth == 0
    assert "skip" not in plan
    assert plan["target_speaker"]["id"] == speakers[0].id
    assert plan["source_speaker_exists"] is True
    assert plan["affected_profile_ids"] == set()
    for value in plan.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))


# --------------------------------------------------------------------------- #
# 4. rediarize — the cloud post stage (issue #1225): pyannote.ai upload + poll
# --------------------------------------------------------------------------- #
def test_rediarize_cloud_engine_holds_no_transaction_across_the_vendor_call(
    db_session, normal_user, monkeypatch
):
    """The post stage reads (file, segments, the key), then talks to the vendor with NO
    scope open, then writes in its own short scope."""
    from app.services.diarization.factory import DiarizationProviderFactory
    from app.services.diarization.types import DiarizeResult
    from app.services.diarization.types import DiarizeSegment
    from app.tasks import rediarize_task as rdz

    tracker = ScopeTracker(db_session)
    media_file, _speakers = make_transcribed_file(db_session, normal_user)
    monkeypatch.setattr(rdz, "session_scope", tracker.scope)

    def _audio(*_a, **_k):
        tracker.observe("prepare_audio")
        return "/scratch/audio.wav", None

    class _VendorDetector:
        def diarize(self, audio_path, config, progress_callback=None):
            tracker.observe("vendor_http")
            return DiarizeResult(
                segments=[
                    DiarizeSegment(start=0.0, end=8.0, speaker="SPEAKER_00"),
                    DiarizeSegment(start=8.0, end=16.0, speaker="SPEAKER_01"),
                ],
                num_speakers=2,
                provider_name="pyannote",
                model_name="precision-3",
            )

    def _build(engine, user_id, db, *, config_uuid=None):
        tracker.observe("build_detector")
        return _VendorDetector()

    monkeypatch.setattr(rdz, "_prepare_audio", _audio)
    monkeypatch.setattr(DiarizationProviderFactory, "create_for_engine", staticmethod(_build))
    monkeypatch.setattr(rdz, "_after_speakers_changed", lambda *a, **k: None)
    for name in ("send_progress_notification", "send_completion_notification"):
        monkeypatch.setattr(f"app.tasks.transcription.notifications.{name}", lambda *a, **k: None)

    result = rdz.rediarize_task.apply(
        kwargs={"file_uuid": str(media_file.uuid), "engine": "pyannote_ai", "merge": "segment"}
    ).get()

    assert result["status"] == "success", result
    observed = tracker.seen
    assert observed["prepare_audio"] == 0, leak(tracker, "prepare_audio")
    assert observed["vendor_http"] == 0, leak(tracker, "vendor_http")
    # Building the detector reads the stored key: that one belongs inside a scope.
    assert observed["build_detector"] == 1, tracker.observations
    assert tracker.opened >= 2
    assert tracker.max_depth == 1, "session scopes must not nest"


# --------------------------------------------------------------------------- #
# 10. transcription.embeddings v4 staging — OpenSearch search + write per profile
# --------------------------------------------------------------------------- #
def test_v4_profile_update_hits_opensearch_outside_the_session(
    db_session, normal_user, monkeypatch
):
    from app.models.speaker import SpeakerProfile
    from app.services import opensearch_service as oss

    tracker = ScopeTracker(db_session)
    stored: list[dict] = []

    monkeypatch.setattr(temb, "session_scope", tracker.scope)

    media_file, speakers = make_transcribed_file(db_session, normal_user, speakers=1)
    profile = SpeakerProfile(
        uuid=uuid_mod.uuid4(), user_id=normal_user.id, name=f"P-{uuid_mod.uuid4().hex[:6]}"
    )
    db_session.add(profile)
    db_session.flush()
    speakers[0].profile_id = profile.id
    db_session.flush()

    class _FakeOSClient:
        def search(self, index, body):
            tracker.observe("opensearch_search")
            return {"hits": {"hits": [{"_id": "other", "_source": {"embedding": [0.0, 0.0, 1.0]}}]}}

    monkeypatch.setattr(oss, "get_opensearch_client", lambda: _FakeOSClient())

    def _store(**kwargs):
        tracker.observe("store_profile_embedding_v4")
        stored.append(kwargs)

    monkeypatch.setattr(oss, "store_profile_embedding_v4", _store)

    updated = temb._update_v4_profile_embeddings(
        {int(profile.id)},
        {speakers[0].name: np.array([1.0, 0.0, 0.0])},
        {speakers[0].name: int(speakers[0].id)},
        set(),
    )

    assert updated == 1
    assert tracker.seen["opensearch_search"] == 0, leak(tracker, "opensearch_search")
    assert tracker.seen["store_profile_embedding_v4"] == 0, leak(
        tracker, "store_profile_embedding_v4"
    )
    assert tracker.opened == 1, f"expected exactly one read scope, got {tracker.opened}"
    assert tracker.depth == 0

    # The read phase supplied the profile identity, so "nothing was sent" cannot
    # satisfy the assertions above.
    assert stored[0]["profile_id"] == int(profile.id)
    assert stored[0]["profile_uuid"] == str(profile.uuid)
    assert stored[0]["speaker_count"] == 2  # current file + one existing v4 doc
    assert np.allclose(stored[0]["embedding"], [0.70710678, 0.0, 0.70710678])


# ---------------------------------------------------------------------------------------
# Cloud speaker detection via another saved provider (#1226)
# ---------------------------------------------------------------------------------------


def test_another_providers_speaker_detection_calls_the_vendor_outside_the_session(
    db_session, normal_user, monkeypatch
):
    """The adapter is BUILT in the factory's read session (no network) and the vendor is
    called with no session open; both legs of the parallel cloud path are observed."""
    from app.models.user_asr_settings import UserASRSettings
    from app.services.asr.base import ASRProvider
    from app.services.asr.factory import ASRProviderFactory
    from app.services.asr.types import ASRResult
    from app.services.asr.types import ASRSegment
    from app.services.asr.types import ASRWord
    from app.services.pipeline_plan import DiarizerConfigInfo
    from app.tasks.transcription import cloud_asr
    from app.tasks.transcription.context import TranscriptionContext
    from app.utils.encryption import encrypt_api_key
    from tests.unit._plan_contexts import make_plan

    tracker = ScopeTracker(db_session)
    monkeypatch.setattr(cloud_asr, "session_scope", tracker.scope)
    monkeypatch.setattr(cloud_asr, "send_progress_notification", lambda *a, **k: None)

    class _Vendor(ASRProvider):
        def __init__(self, name: str, label: str, speaker: str | None) -> None:
            self._name, self._label, self._speaker = name, label, speaker

        @property
        def provider_name(self) -> str:
            return self._name

        def supports_diarization(self) -> bool:
            return self._speaker is not None

        def supports_vocabulary(self) -> bool:
            return False

        def supports_translation(self) -> bool:
            return False

        def validate_connection(self) -> tuple[bool, str, float]:
            return True, "ok", 0.0

        def transcribe(self, audio_path, config, progress_callback=None) -> ASRResult:
            tracker.observe(self._label)
            words = [ASRWord("hello", 0.0, 0.5), ASRWord("there", 0.6, 1.0)]
            segment = ASRSegment("hello there", 0.0, 1.0, speaker=self._speaker, words=words)
            return ASRResult(
                segments=[segment], language="en", provider_name=self._name, model_name="nova-3"
            )

    diarizer = UserASRSettings(
        user_id=normal_user.id,
        name=f"speakers-{uuid_mod.uuid4().hex[:8]}",
        provider="gladia",
        model_name="standard",
        api_key=encrypt_api_key("gladia-key"),
    )
    db_session.add(diarizer)
    db_session.commit()
    plan = make_plan(
        "cloud_text_word",
        "asr_config",
        diarization_config=DiarizerConfigInfo(
            provider="gladia", model="standard", uuid=str(diarizer.uuid), returns_speakers=True
        ),
    )
    assert plan.diarization.execution == "parallel_cloud"
    task_id = f"task-{uuid_mod.uuid4().hex[:8]}"
    db_session.add(
        TaskModel(
            id=task_id,
            user_id=normal_user.id,
            media_file_id=None,
            task_type="transcription",
            status="in_progress",
            progress=0.1,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        ASRProviderFactory,
        "create_from_db_config",
        staticmethod(lambda cfg: _Vendor("gladia", "vendor_speakers", "SPEAKER_01")),
    )
    ctx = TranscriptionContext(
        task_id=task_id,
        file_id=1,
        file_uuid=str(uuid_mod.uuid4()),
        user_id=normal_user.id,
        file_path="x/a.mp3",
        file_name="a.mp3",
        content_type="audio/mpeg",
    )

    result = cloud_asr._run_cloud_asr_pipeline(
        ctx,
        "/fake/audio.wav",
        None,
        None,
        None,
        provider=_Vendor("deepgram", "vendor_text", None),
        plan=plan,
    )

    assert tracker.opened >= 2
    assert tracker.seen == {"vendor_text": 0, "vendor_speakers": 0}, leak(tracker, "vendor")
    assert result["diarization_provider"] == "gladia"
    assert {w["speaker"] for w in result["segments"][0]["words"]} == {"SPEAKER_01"}
