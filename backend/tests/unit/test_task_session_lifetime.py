# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (a fake session, a recording Path) to
# signatures that declare Session/Path. Declared once here rather than as a cast at
# every call site — a cast buries the thing being asserted, and widening a production
# signature to suit a test is worse. Same convention as
# tests/unit/test_proxy_identity_consistency.py.
"""Celery tasks must not hold a DB transaction across their slow phase.

Sibling of ``test_speaker_attribute_session_lifetime.py``, which documents the
measured incident: a CPU worker sat ``idle in transaction`` for 48+ minutes with
``transcript_segment`` under ACCESS SHARE because one ``session_scope()`` wrapped
a MinIO presign, a wav2vec2 load and eight ffmpeg subprocesses. That transaction
queues every ``ALTER TABLE`` (i.e. any Alembic upgrade), pins the vacuum horizon
on the largest table in the product, and burns a pool connection for its whole
life.

An AST sweep found the same shape in four more tasks. This module covers them:

===============================================  ====================================
task                                             slow work formerly inside the session
===============================================  ====================================
``extract_speaker_embeddings``                   MinIO download + ffmpeg + embedding model
``update_speaker_embedding_on_reassignment``     MinIO download + ffmpeg + inference
``transcription.embeddings``                     per-speaker ffmpeg slices + inference
``reindex_batch``                                50 OpenSearch re-index round trips
``watch_source.stitch_and_import``               SMB/S3 part downloads + ffmpeg concat
``watch_source.send_notification``               SMTP send (30 s timeout per config)
``ai.generate_summary``                          LLM completion over a whole transcript
``ai.identify_speakers``                         LLM ``identify_speakers`` call
``watch_source.scan_single``                     remote list + per-file download (**interprocedural**)
``download.prepare_media``                       MinIO download + full ffmpeg transcode
``cleanup_expired_files``                        MinIO + OpenSearch deletes, per expired file
``transcription.embeddings`` (v4 staging)        OpenSearch search + write, per profile
===============================================  ====================================

The last six were the second sweep. Two of them were **actively wedging the
database** when they were found (the NLP worker idle-in-transaction for 1 h 26 m
on the summarization SELECT), and ``scan_single`` is the one an AST *body* scan
cannot see: its ``session_scope`` wraps ``_perform_scan``, and the transfers are
a frame further down. ``scripts/audit-session-lifetime.py`` exists because of
that case — it has an explicit interprocedural rule.

The tests are behavioural, not structural: each swaps the module's
``session_scope`` for a depth-tracking stand-in over the savepointed
``db_session``, creates real rows, and has the stub for each slow call report the
open-scope depth *at the moment it runs*. Every test also asserts that at least
two scopes were opened, so a task that never touches the DB cannot pass.
"""

import contextlib
import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from celery.exceptions import Retry

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Task as TaskModel
from app.models.media import TranscriptSegment
from app.models.topic import TopicSuggestion
from app.models.watch_source import WatchSource
from app.tasks import media_download as mdl
from app.tasks import reindex_task as rix
from app.tasks import speaker_embedding_task as seb
from app.tasks import watch_source_tasks as wst
from app.tasks.transcription import embeddings as temb


class _ScopeTracker:
    """Stands in for ``session_scope``, recording how many scopes are open."""

    def __init__(self, session):
        self._session = session
        self.depth = 0
        self.max_depth = 0
        self.opened = 0
        #: (label, depth) reported from inside each slow call.
        self.observations: list[tuple[str, int]] = []
        #: (label, depth, scopes-opened-so-far) for the same calls.
        self.timeline: list[tuple[str, int, int]] = []
        #: Per-task payloads the fixtures hang here so a test can assert on what the
        #: slow phase actually received (the aggregated vector, the indexed uuids, ...).
        #: Declared rather than bolted on dynamically so mypy can see them.
        self.recorded: Any = None
        self.indexed: Any = None

    @contextlib.contextmanager
    def scope(self):
        self.depth += 1
        self.opened += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            yield self._session
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            self.depth -= 1

    def observe(self, label: str) -> None:
        self.observations.append((label, self.depth))
        #: The scope COUNT at the moment of the call. Two observations sharing a
        #: count ran inside the same scope; different counts prove they did not.
        #: Depth alone cannot tell those apart.
        self.timeline.append((label, self.depth, self.opened))

    def opened_at(self, label: str) -> list[int]:
        """Scope counts at each occurrence of ``label``."""
        return [count for name, _depth, count in self.timeline if name == label]

    @property
    def seen(self) -> dict[str, int]:
        return dict(self.observations)


def _leak(tracker: _ScopeTracker, label: str) -> str:
    return f"a DB transaction was held across the slow phase ({label}): {tracker.observations}"


def _make_transcribed_file(db_session, user, *, speakers: int = 2):
    """A media file with speakers and segments, as after a completed transcription."""
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename="meeting.mp4",
        storage_path="test/meeting.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()

    created = []
    start = 0.0
    for i in range(speakers):
        speaker = Speaker(
            uuid=str(uuid_mod.uuid4()),
            media_file_id=media_file.id,
            user_id=user.id,
            name=f"SPEAKER_0{i}",
        )
        db_session.add(speaker)
        db_session.flush()
        created.append(speaker)

        for _ in range(2):
            db_session.add(
                TranscriptSegment(
                    uuid=str(uuid_mod.uuid4()),
                    media_file_id=media_file.id,
                    speaker_id=speaker.id,
                    start_time=start,
                    end_time=start + 4.0,
                    text="hello there",
                )
            )
            start += 4.0
    db_session.flush()
    return media_file, created


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

    def cleanup(self):
        self._tracker.observe("model_cleanup")


@pytest.fixture
def embedding_task_env(db_session, monkeypatch):
    """Patch ``speaker_embedding_task``'s slow seams so they are observable and instant."""
    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(seb, "session_scope", tracker.scope)

    # --- audio staging (MinIO) ---
    def _temp_exists(file_uuid):
        tracker.observe("temp_audio_exists")
        return True

    monkeypatch.setattr("app.services.minio_service.temp_audio_exists", _temp_exists)
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
    media_file, speakers = _make_transcribed_file(db_session, normal_user)
    speaker_mapping = {s.name: s.id for s in speakers}

    async_result = seb.extract_speaker_embeddings_task.apply(
        args=[str(media_file.uuid), speaker_mapping]
    )
    result = async_result.get()

    assert result["status"] == "success", result

    observed = tracker.seen
    for label in (
        "temp_audio_exists",
        "download_temp_audio",
        "extract_embeddings",
        "gpu_sync",
        "model_cleanup",
    ):
        assert observed[label] == 0, _leak(tracker, label)

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

    # And the task still records completion.
    task_row = db_session.query(TaskModel).filter(TaskModel.id == async_result.task_id).first()
    assert task_row is not None and task_row.status == "completed"
    assert result["speakers_processed"] == len(speakers)


def test_extract_speaker_embeddings_releases_scope_when_inference_fails(
    db_session, normal_user, embedding_task_env, monkeypatch
):
    """A failure in the slow phase must not leave a scope open either."""
    tracker = embedding_task_env
    media_file, speakers = _make_transcribed_file(db_session, normal_user)

    def boom(self, audio_path, segments, speaker_mapping):
        tracker.observe("extract_embeddings")
        raise RuntimeError("ffmpeg segment fetch failed")

    monkeypatch.setattr(_FakeEmbeddingService, "extract_embeddings_for_segments", boom)

    result = seb.extract_speaker_embeddings_task.apply(
        args=[str(media_file.uuid), {s.name: s.id for s in speakers}]
    ).get()

    assert result["status"] == "error"
    assert tracker.seen["extract_embeddings"] == 0, _leak(tracker, "extract_embeddings")
    assert tracker.depth == 0, "a session scope survived the failure"


def test_speaker_embedding_read_phase_returns_plain_data(
    db_session, normal_user, embedding_task_env
):
    """``_load_speaker_embedding_inputs`` must not hand back live ORM instances.

    Returning ORM objects would push lazy loads — and therefore a new
    transaction — into the slow phase, reintroducing the leak by the back door.
    """
    tracker = embedding_task_env
    media_file, speakers = _make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = seb._load_speaker_embedding_inputs(str(media_file.uuid), str(uuid_mod.uuid4()))

    assert tracker.depth == 0
    assert inputs["file_id"] == media_file.id
    assert inputs["user_id"] == normal_user.id
    assert inputs["storage_path"] == "test/meeting.mp4"
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
    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(seb, "session_scope", tracker.scope)

    def _download(storage_path):
        tracker.observe("minio_download")
        import io

        return io.BytesIO(b"fake-media"), "video/mp4", 10

    monkeypatch.setattr("app.services.minio_service.download_file", _download)

    def _prepare(temp_file_path, content_type, temp_dir):
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
    media_file, speakers = _make_transcribed_file(db_session, normal_user)
    segment = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id == speakers[1].id)
        .first()
    )

    result = seb.update_speaker_embedding_on_reassignment.apply(
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
        assert observed[label] == 0, _leak(tracker, label)
    # The OpenSearch phase needs no DB session at all.
    for label in ("opensearch_get", "opensearch_write"):
        assert observed[label] == 0, _leak(tracker, label)

    assert tracker.opened >= 1
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    stored = tracker.recorded["add_speaker_embedding"]
    assert stored["speaker_uuid"] == str(speakers[1].uuid)
    assert stored["speaker_id"] == speakers[1].id
    assert stored["segment_count"] == 1


def test_reassignment_read_phase_returns_plain_data(db_session, normal_user, reassignment_env):
    tracker = reassignment_env
    _, speakers = _make_transcribed_file(db_session, normal_user)
    segment = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id == speakers[0].id)
        .first()
    )

    plan = seb._load_reassignment_plan(
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
# 4. transcription/embeddings — the in-pipeline copy of the same call
# --------------------------------------------------------------------------- #
def test_transcription_embeddings_extracts_outside_the_session(
    db_session, normal_user, monkeypatch
):
    from app.tasks.transcription.context import TranscriptionContext

    tracker = _ScopeTracker(db_session)
    recorded: dict = {}
    media_file, speakers = _make_transcribed_file(db_session, normal_user)

    monkeypatch.setattr(temb, "session_scope", tracker.scope)
    monkeypatch.setattr(
        temb, "update_task_status", lambda *a, **kw: tracker.observe("update_task_status")
    )
    monkeypatch.setattr(
        "app.utils.hardware_detection.detect_hardware", lambda: _FakeHardware(tracker)
    )
    monkeypatch.setattr(
        "app.services.speaker_embedding_service.get_cached_embedding_service",
        lambda: _FakeEmbeddingService(tracker),
    )

    class _FakeMatching:
        def __init__(self, db, embedding_service=None):
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
            # See the note on the sibling fake: present only so the control
            # revert fails on scope depth rather than on an AttributeError.
            raw = self.embedding_service.extract_embeddings_for_segments(
                audio_path, segments, speaker_mapping
            )
            tracker.observe("speaker_matching")
            recorded["native_embeddings"] = {
                sid: self.embedding_service.aggregate_embeddings(embs) for sid, embs in raw.items()
            }
            return [{"speaker_id": sid} for sid in raw]

    monkeypatch.setattr(temb, "SpeakerMatchingService", _FakeMatching)

    ctx = TranscriptionContext(
        task_id=str(uuid_mod.uuid4()),
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=normal_user.id,
        file_path="test/meeting.mp4",
        file_name="meeting.mp4",
        content_type="video/mp4",
    )
    speaker_mapping = {s.name: s.id for s in speakers}

    temb._process_speaker_embeddings(
        ctx,
        "does-not-matter.wav",
        [{"speaker": s.name, "start": 0.0, "end": 4.0} for s in speakers],
        speaker_mapping,
    )

    observed = tracker.seen
    assert observed["extract_embeddings"] == 0, _leak(tracker, "extract_embeddings")
    assert observed["gpu_sync"] == 0, _leak(tracker, "gpu_sync")
    assert observed["speaker_matching"] == 1, tracker.observations
    assert observed["update_task_status"] == 1, tracker.observations
    assert tracker.opened >= 1
    assert tracker.max_depth == 1

    assert set(recorded["native_embeddings"]) == set(speaker_mapping.values())
    assert np.allclose(next(iter(recorded["native_embeddings"].values())), [0.6, 0.8, 0.0])


# --------------------------------------------------------------------------- #
# 5. reindex_batch — 50 OpenSearch round trips per page
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self):
        self.hashes: dict[tuple[str, str], int] = {}
        self.sets: dict[str, set] = {}
        self.expiries: dict[str, int] = {}

    def hget(self, key, field):
        return self.hashes.get((key, field))

    def hset(self, key, field, value):
        self.hashes[(key, field)] = value

    def hincrby(self, key, field, amount=1):
        value = int(self.hashes.get((key, field)) or 0) + amount
        self.hashes[(key, field)] = value
        return value

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def expire(self, key, seconds):
        self.expiries[key] = seconds
        return key in self.sets or any(k == key for k, _ in self.hashes)


@pytest.fixture
def reindex_env(db_session, monkeypatch):
    """Patch ``reindex_task``'s Redis/progress/indexing seams; keep the DB real."""
    tracker = _ScopeTracker(db_session)
    indexed: list[str] = []

    monkeypatch.setattr(rix, "session_scope", tracker.scope)
    monkeypatch.setattr(rix, "get_redis", lambda: _FakeRedis())
    monkeypatch.setattr(rix, "_send_reindex_progress", lambda *a, **kw: None)
    monkeypatch.setattr(rix, "_is_cancellation_requested", lambda user_id: False)
    monkeypatch.setattr(rix, "_handle_reindex_completion", lambda *a, **kw: None)

    class _FakeTracker:
        def __init__(self, **kwargs):
            pass

        @staticmethod
        def get_state(task_type, user_id):
            return None

        def start(self, message=""):
            pass

        def resume_from_state(self, state):
            pass

    monkeypatch.setattr("app.services.progress_tracker.ProgressTracker", _FakeTracker)

    class _FakeIndexing:
        def reindex_transcript(self, **kwargs):
            # A full re-chunk + embed + bulk write per file. Fifty of these used
            # to run inside one transaction.
            tracker.observe("reindex_transcript")
            indexed.append(kwargs["file_uuid"])
            return 7

    monkeypatch.setattr(
        "app.services.search.indexing_service.TranscriptIndexingService", _FakeIndexing
    )

    tracker.indexed = indexed
    return tracker


def test_reindex_batch_indexes_outside_the_session(db_session, normal_user, reindex_env):
    tracker = reindex_env
    indexed = tracker.indexed
    files = [_make_transcribed_file(db_session, normal_user, speakers=1)[0] for _ in range(3)]

    stats = rix.reindex_batch_task([f.id for f in files], normal_user.id)

    assert stats["indexed"] == 3, stats
    assert stats["chunks"] == 21
    assert sorted(indexed) == sorted(str(f.uuid) for f in files)

    depths = [depth for label, depth in tracker.observations if label == "reindex_transcript"]
    assert depths == [0, 0, 0], _leak(tracker, "reindex_transcript")
    assert tracker.opened >= 1, "the read phase must actually have opened a session"
    assert tracker.max_depth == 1


def test_reindex_page_loader_returns_plain_data(db_session, normal_user, monkeypatch):
    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(rix, "session_scope", tracker.scope)
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    page = rix._load_reindex_page([media_file.id])

    assert tracker.depth == 0
    assert len(page) == 1
    file_uuid, metadata = page[0]
    assert file_uuid == str(media_file.uuid)
    assert metadata is not None
    assert len(metadata["segments"]) == 2
    assert all(isinstance(seg, dict) for seg in metadata["segments"])
    for value in metadata.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))


# --------------------------------------------------------------------------- #
# 3. watch_source.stitch_and_import / send_notification
# --------------------------------------------------------------------------- #
def _make_watch_source(db_session, user, *, source_type="s3"):
    source = WatchSource(
        uuid=uuid_mod.uuid4(),
        name=f"src-{uuid_mod.uuid4().hex[:8]}",
        source_type=source_type,
        user_id=user.id,
        created_by=user.id,
        auto_transcribe=False,
    )
    db_session.add(source)
    db_session.flush()
    return source


@pytest.fixture
def stitch_env(db_session, normal_user, monkeypatch, tmp_path):
    """Patch the watch-source transfer/ffmpeg/ingest seams; keep the DB real."""
    from app.core.config import settings as app_settings

    tracker = _ScopeTracker(db_session)
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(wst, "session_scope", tracker.scope)
    monkeypatch.setattr(
        type(app_settings), "watch_temp_dir", property(lambda self: tmp_path), raising=False
    )

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def download_file(self, remote_path, local_path):
            # A multi-part group is gigabytes over SMB/S3.
            tracker.observe("part_download")
            with open(local_path, "wb") as fh:
                fh.write(b"part")
            return 4

        def upload_file(self, local_path, remote_path):
            tracker.observe("upload_back")
            return True

    monkeypatch.setattr(wst, "create_client", lambda src: _FakeClient())

    def _stitch(parts, output_path):
        tracker.observe("ffmpeg_stitch")
        with open(output_path, "wb") as fh:
            fh.write(b"stitched")
        return True

    monkeypatch.setattr("app.services.watch_sources.multipart.stitch_files", _stitch)
    monkeypatch.setattr(
        "app.services.watch_sources.multipart.generate_stitched_filename",
        lambda base, ext: f"{base}_stitched{ext}",
    )

    def _ingest(db, src, local_path, *, filename, row, size=None):
        # Legitimately DB work — it creates the MediaFile row.
        tracker.observe("ingest_prepared_file")
        row.status = "imported"
        row.media_file_id = media_file.id
        return row

    monkeypatch.setattr(wst, "ingest_prepared_file", _ingest)
    return tracker


def test_stitch_and_import_downloads_and_concats_outside_the_session(
    db_session, normal_user, stitch_env
):
    tracker = stitch_env
    source = _make_watch_source(db_session, normal_user)

    parts = [
        {"path": "remote/clip.part1.mp4", "name": "clip.part1.mp4", "size": 4, "modified": None},
        {"path": "remote/clip.part2.mp4", "name": "clip.part2.mp4", "size": 4, "modified": None},
    ]
    result = wst.stitch_and_import.apply(args=[source.id, "clip", ".mp4", parts]).get()

    assert result["status"] == "imported", result

    download_depths = [d for label, d in tracker.observations if label == "part_download"]
    assert download_depths == [0, 0], _leak(tracker, "part_download")
    assert tracker.seen["ffmpeg_stitch"] == 0, _leak(tracker, "ffmpeg_stitch")
    # The ingest itself is DB work and belongs inside a (short) scope.
    assert tracker.seen["ingest_prepared_file"] == 1, tracker.observations
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"


def test_send_notification_sends_mail_outside_the_session(db_session, normal_user, monkeypatch):
    tracker = _ScopeTracker(db_session)
    source = _make_watch_source(db_session, normal_user, source_type="local")

    config = EmailNotificationConfig(
        uuid=uuid_mod.uuid4(),
        name=f"mailer-{uuid_mod.uuid4().hex[:8]}",
        provider="smtp",
        smtp_host="smtp.invalid.example.com",
        smtp_port=587,
        from_address="noreply@example.com",
        default_recipients="ops@example.com",
        is_enabled=True,
    )
    db_session.add(config)
    db_session.flush()
    db_session.add(
        WatchSourceEmail(
            watch_source_id=source.id,
            email_config_id=config.id,
            notify_on_success=True,
            notify_on_error=True,
        )
    )
    db_session.flush()

    sends: list[tuple] = []
    lazy_loads: list[str] = []

    def _send_email(cfg, recipients, subject, html_body, timeout=30):
        # SMTP/Graph round trip: 30 s timeout per config.
        tracker.observe("send_email")
        # Reading a column off the detached config must still work...
        sends.append((cfg.smtp_host, tuple(recipients), subject))
        # ...and a lazy relationship load must fail LOUDLY rather than quietly
        # opening a second transaction while the socket is open.
        try:
            _ = cfg.links[0]
            lazy_loads.append("succeeded")
        except Exception as exc:
            lazy_loads.append(type(exc).__name__)
        return True, "ok"

    monkeypatch.setattr(wst, "session_scope", tracker.scope)
    monkeypatch.setattr("app.services.watch_email_service.send_email", _send_email)

    result = wst.send_notification.apply(
        args=[source.id, {"found": 1, "imported": 1, "skipped": 0, "errors": 0}]
    ).get()

    assert result == {"sent": 1}
    assert tracker.seen["send_email"] == 0, _leak(tracker, "send_email")
    assert tracker.opened >= 1
    assert sends[0][0] == "smtp.invalid.example.com"
    assert sends[0][1] == ("ops@example.com",)
    # The config reaches send_email DETACHED, so a stray lazy load raises
    # instead of silently opening a transaction mid-SMTP.
    assert lazy_loads == ["DetachedInstanceError"], lazy_loads


# --------------------------------------------------------------------------- #
# 2. download.prepare_media — MinIO download + full ffmpeg transcode
#
#    The residual this test used to document is gone:
#    ``VideoProcessingService.extract_audio`` / ``process_video_with_subtitles``
#    no longer take a ``Session``. They open their own short sessions for the two
#    reads they need (the filename, and the transcript for the SRT), so the task
#    holds nothing across the transcode.
# --------------------------------------------------------------------------- #
def test_prepare_media_download_transcodes_outside_the_session(
    db_session, normal_user, monkeypatch
):
    tracker = _ScopeTracker(db_session)
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(mdl, "session_scope", tracker.scope)
    monkeypatch.setattr(mdl, "publish_download_event", lambda *a, **kw: None)
    monkeypatch.setattr(mdl, "release_download_prep_guard", lambda *a, **kw: None)
    monkeypatch.setattr(mdl, "MinIOService", lambda *a, **kw: object())

    class _FakeService:
        def __init__(self, minio):
            pass

        def extract_audio(self, *, file_id, original_object_name, audio_format):
            # A MinIO download plus a full ffmpeg transcode: minutes.
            tracker.observe("extract_audio")
            return "cache/key.mp3", "mp3", "audio/mpeg"

        def presigned_download_url(self, cache_key, download_filename, content_type):
            tracker.observe("presign")
            return "http://minio.invalid/key.mp3"

    monkeypatch.setattr(mdl, "VideoProcessingService", _FakeService)

    result = mdl.prepare_media_download_task.apply(
        args=[media_file.id, normal_user.id, "audio_mp3"]
    ).get()

    assert result["status"] == "success", result
    assert tracker.seen["extract_audio"] == 0, _leak(tracker, "extract_audio")
    assert tracker.seen["presign"] == 0, _leak(tracker, "presign")
    assert tracker.opened == 1, f"expected exactly the read scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0


def test_video_service_reads_use_their_own_short_sessions(db_session, normal_user, monkeypatch):
    """``VideoProcessingService`` opens (and closes) its own scope per read.

    Without this, "the task holds no session" above would be satisfied by the
    service holding one instead — the leak moved, not removed.
    """
    from app.services import video_processing_service as vps

    tracker = _ScopeTracker(db_session)
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(vps, "session_scope", tracker.scope)

    filename = vps.VideoProcessingService._media_filename(int(media_file.id))

    assert filename == "meeting.mp4"
    assert tracker.opened == 1
    assert tracker.depth == 0, "the filename read left a scope open"

    written: dict = {}

    class _FakeSubtitles:
        @staticmethod
        def generate_srt_content(db, file_id, include_speakers):
            written["depth_during_render"] = tracker.depth
            return "1\n00:00:00,000 --> 00:00:04,000\nhello there\n"

    monkeypatch.setattr(vps, "SubtitleService", _FakeSubtitles)

    class _RecordingPath:
        """Reports the open-scope depth at the moment the SRT is written."""

        def write_text(self, content, encoding=None):
            written["depth_during_write"] = tracker.depth
            written["content"] = content

    service = vps.VideoProcessingService.__new__(vps.VideoProcessingService)
    service._generate_subtitle_file(int(media_file.id), _RecordingPath(), True)

    # The transcript read needs a session; the FILE WRITE that follows must not
    # still be inside it, and neither may the ffmpeg run after that.
    assert written["depth_during_render"] == 1
    assert written["depth_during_write"] == 0, _leak(tracker, "srt_write")
    assert "hello there" in written["content"]
    assert tracker.depth == 0
    assert tracker.opened == 2
    assert tracker.max_depth == 1


# --------------------------------------------------------------------------- #
# 6. ai.generate_summary — an LLM completion over the WHOLE transcript
#
#    This is one of the two that were found holding a live transaction: the NLP
#    worker sat idle-in-transaction for 1 h 26 m with the transcript_segment
#    SELECT below as its last statement.
# --------------------------------------------------------------------------- #
class _FakeLLMConfig:
    provider = "fake"
    model = "fake-1"


class _FakeLLMService:
    """Stands in for the provider client. Records what it was actually SENT."""

    user_context_window = 32768
    config = _FakeLLMConfig()

    def __init__(self, tracker, recorded, *, boom: bool = False):
        self._tracker = tracker
        self._recorded = recorded
        self._boom = boom
        self.closed = False

    def generate_summary(self, **kwargs):
        # A multi-minute HTTP round trip against an external provider.
        self._tracker.observe("llm_generate_summary")
        self._recorded["summary_kwargs"] = kwargs
        if self._boom:
            raise RuntimeError("provider returned 502")
        return {"bluf": "They agreed.", "brief_summary": "A meeting.", "metadata": {}}

    def identify_speakers(self, **kwargs):
        self._tracker.observe("llm_identify_speakers")
        self._recorded["identify_kwargs"] = kwargs
        if self._boom:
            raise RuntimeError("provider returned 502")
        return {
            "speaker_predictions": [
                {"speaker_label": "SPEAKER_00", "predicted_name": "Ada", "confidence": 0.9},
                {"speaker_label": "SPEAKER_01", "predicted_name": "Grace", "confidence": 0.2},
            ],
            "overall_confidence": "medium",
        }

    def close(self):
        self.closed = True


@pytest.fixture
def summarization_env(db_session, monkeypatch):
    """Patch summarization's LLM and notification seams; keep the DB real.

    There is no OpenSearch seam to patch any more: the task writes
    ``media_file.summary_data`` and nothing else since ``transcript_summaries``
    was retired (#67).
    """
    from app.tasks import summarization as summ

    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(summ, "session_scope", tracker.scope)
    monkeypatch.setattr(summ, "resolve_llm_masking", lambda db, media_file: None)
    monkeypatch.setattr(
        summ,
        "send_summary_notification",
        lambda *a, **kw: recorded.setdefault("notifications", []).append(a),
    )
    service = _FakeLLMService(tracker, recorded)

    class _Factory:
        @staticmethod
        def create_from_user_settings(user_id):
            recorded["llm_user_id"] = user_id
            return service

        @staticmethod
        def create_from_system_settings():
            return service

    monkeypatch.setattr(summ, "LLMService", _Factory)

    tracker.recorded = recorded
    tracker.llm_service = service  # type: ignore[attr-defined]
    return tracker


def test_summarization_calls_the_llm_outside_the_session(
    db_session, normal_user, summarization_env
):
    """The regression: the provider round trip must run with zero scopes open."""
    from app.tasks import summarization as summ

    tracker = summarization_env
    media_file, _ = _make_transcribed_file(db_session, normal_user)

    result = summ.summarize_transcript_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "success", result

    observed = tracker.seen
    assert observed["llm_generate_summary"] == 0, _leak(tracker, "llm_generate_summary")

    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase really did produce the transcript the provider was sent,
    # so "zero scopes open" cannot be satisfied by sending nothing.
    sent = tracker.recorded["summary_kwargs"]
    assert "hello there" in sent["transcript"]
    assert sent["user_id"] == normal_user.id
    assert tracker.llm_service.closed is True

    # And the result landed in Postgres.
    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed.summary_status == "completed"
    assert refreshed.summary_data["bluf"] == "They agreed."


def test_summarization_read_phase_returns_plain_data(db_session, normal_user, summarization_env):
    """``_load_summarization_inputs`` must not hand back live ORM instances."""
    from app.tasks import summarization as summ

    tracker = summarization_env
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = summ._load_summarization_inputs(
        str(media_file.uuid), str(uuid_mod.uuid4()), force_regenerate=False
    )

    assert tracker.depth == 0
    assert inputs["file_id"] == media_file.id
    assert inputs["user_id"] == normal_user.id
    assert isinstance(inputs["full_transcript"], str) and inputs["full_transcript"]
    assert isinstance(inputs["speaker_stats"], dict) and inputs["speaker_stats"]
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))


def test_summarization_releases_the_scope_when_the_provider_fails(
    db_session, normal_user, summarization_env, monkeypatch
):
    """A provider failure must not leave a scope open — and must mark the file failed."""
    from app.tasks import summarization as summ

    tracker = summarization_env
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)
    tracker.llm_service._boom = True  # type: ignore[attr-defined]

    result = summ.summarize_transcript_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "error", result
    assert tracker.seen["llm_generate_summary"] == 0, _leak(tracker, "llm_generate_summary")
    assert tracker.depth == 0, "a session scope survived the failure"

    db_session.expire_all()
    refreshed = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).first()
    assert refreshed.summary_status == "failed"


# --------------------------------------------------------------------------- #
# 7. ai.identify_speakers — the other one that was actively wedging the DB
# --------------------------------------------------------------------------- #
@pytest.fixture
def identification_env(db_session, monkeypatch):
    from app.tasks import speaker_identification_task as sid

    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    monkeypatch.setattr(sid, "session_scope", tracker.scope)
    monkeypatch.setattr(sid, "resolve_llm_masking", lambda db, media_file: None)
    monkeypatch.setattr(sid, "send_ws_event", lambda *a, **kw: None)

    service = _FakeLLMService(tracker, recorded)
    monkeypatch.setattr(sid, "_create_llm_service", lambda user_id: service)

    tracker.recorded = recorded
    tracker.llm_service = service  # type: ignore[attr-defined]
    return tracker


def test_speaker_identification_calls_the_llm_outside_the_session(
    db_session, normal_user, identification_env
):
    from app.tasks import speaker_identification_task as sid

    tracker = identification_env
    media_file, speakers = _make_transcribed_file(db_session, normal_user)

    result = sid.identify_speakers_llm_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "success", result
    assert result["predictions_count"] == 2

    assert tracker.seen["llm_identify_speakers"] == 0, _leak(tracker, "llm_identify_speakers")
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase produced real content for the provider.
    sent = tracker.recorded["identify_kwargs"]
    assert "hello there" in sent["transcript"]
    assert len(sent["speaker_segments"]) == 4
    assert tracker.llm_service.closed is True

    # ...and the confident suggestion was written back, the weak one skipped.
    db_session.expire_all()
    rows = {
        s.name: s.suggested_name
        for s in db_session.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
    }
    assert rows[speakers[0].name] == "Ada"
    assert rows[speakers[1].name] is None  # confidence 0.2 < 0.5


def test_speaker_identification_read_phase_returns_plain_data(
    db_session, normal_user, identification_env
):
    from app.tasks import speaker_identification_task as sid

    tracker = identification_env
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = sid._load_identification_inputs(str(media_file.uuid), str(uuid_mod.uuid4()))

    assert tracker.depth == 0
    assert inputs["file_id"] == media_file.id
    assert isinstance(inputs["full_transcript"], str) and inputs["full_transcript"]
    assert all(isinstance(seg, dict) for seg in inputs["speaker_segments"])
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment))


# --------------------------------------------------------------------------- #
# 8. watch_source.scan_single — the INTERPROCEDURAL one
#
#    The session wrapped ``_perform_scan``, one frame down from the slow calls:
#    ``client.list_files()`` against a remote share, then a download AND a MinIO
#    upload for every file up to ``watch.max_imports_per_scan``.
# --------------------------------------------------------------------------- #
class _FakeLock:
    @contextlib.contextmanager
    def acquire_lock(self, lock_key, timeout=300, blocking_timeout=0):
        yield True


@pytest.fixture
def scan_env(db_session, normal_user, monkeypatch, tmp_path):
    from app.core.config import settings as app_settings
    from app.services.watch_sources import processing as proc
    from app.services.watch_sources.base import RemoteFileInfo

    tracker = _ScopeTracker(db_session)
    recorded: dict = {}
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    # Both modules' scopes are tracked: the leak spans the task AND the service.
    monkeypatch.setattr(wst, "session_scope", tracker.scope)
    monkeypatch.setattr(proc, "session_scope", tracker.scope)
    monkeypatch.setattr(wst, "task_lock_manager", _FakeLock())
    monkeypatch.setattr(wst, "_notify_scan_complete", lambda *a, **kw: None)
    monkeypatch.setattr(
        type(app_settings), "watch_temp_dir", property(lambda self: tmp_path), raising=False
    )

    discovered = [
        RemoteFileInfo(path="remote/talk.mp4", name="talk.mp4", size=4, modified_time=None)
    ]

    class _FakeClient:
        def __init__(self, source):
            # ``LocalWatchClient`` really does keep this reference, which is why
            # the plan must hand over a DETACHED row. Recorded so a test can
            # check the state it was handed over in.
            self.source = source
            recorded["client_source"] = source

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def list_files(self, extensions=None, recursive=True, min_modified=None):
            # An S3 LIST / SMB directory walk over a remote share.
            tracker.observe("list_files")
            return list(discovered)

        def download_file(self, remote_path, local_path):
            # Gigabytes over SMB/S3, per file.
            tracker.observe("part_download")
            with open(local_path, "wb") as fh:
                fh.write(b"data")
            return 4

    monkeypatch.setattr(wst, "create_client", _FakeClient)

    class _Row:
        status = "imported"

    def _ingest(db, source, local_path, *, filename, row, size=None):
        # The documented RESIDUAL: this still runs a MinIO upload inside a
        # session, because the object key derives from the MediaFile PK. What
        # changed is that the scope is now one FILE wide, not one SCAN wide.
        tracker.observe("ingest_prepared_file")
        recorded["ingested"] = filename
        row.status = "imported"
        row.media_file_id = media_file.id
        return _Row()

    monkeypatch.setattr(proc, "ingest_prepared_file", _ingest)

    tracker.recorded = recorded
    return tracker


def test_scan_single_lists_and_downloads_outside_the_session(db_session, normal_user, scan_env):
    from app.models.watch_source import WatchSourceFile

    tracker = scan_env
    source = _make_watch_source(db_session, normal_user, source_type="s3")

    summary = wst.scan_single.apply(args=[source.id]).get()

    assert summary["found"] == 1, summary
    assert summary["imported"] == 1, summary

    observed = tracker.seen
    assert observed["list_files"] == 0, _leak(tracker, "list_files")
    assert observed["part_download"] == 0, _leak(tracker, "part_download")

    # The per-file ingest legitimately needs a session — but its OWN, opened
    # after the download closed. Pinning this stops the fix regressing into
    # "the task stopped touching the DB", which would satisfy the two asserts
    # above for the wrong reason.
    assert observed["ingest_prepared_file"] == 1, tracker.observations
    assert tracker.opened >= 4, (
        "expected separate scopes for the plan, the terminal-path read, the claim, "
        f"the ingest and the result write; got {tracker.opened}"
    )
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The download happened in a LATER scope-generation than the plan read, and
    # the ingest in a later one still: proof they are not one shared session.
    assert tracker.opened_at("list_files")[0] >= 1
    assert tracker.opened_at("part_download")[0] > tracker.opened_at("list_files")[0]
    assert tracker.opened_at("ingest_prepared_file")[0] > tracker.opened_at("part_download")[0]

    # Real rows: the tracking row was claimed and finalized.
    db_session.expire_all()
    row = (
        db_session.query(WatchSourceFile)
        .filter(WatchSourceFile.watch_source_id == source.id)
        .first()
    )
    assert row is not None and row.remote_path == "remote/talk.mp4"

    refreshed = db_session.query(WatchSource).filter(WatchSource.id == source.id).first()
    assert refreshed.last_scan_status == "success"
    assert refreshed.last_scan_files_found == 1


def test_scan_plan_detaches_the_source_row(db_session, normal_user, scan_env):
    """The plan must not hand a live ORM row to the client.

    ``LocalWatchClient`` keeps a reference to the ``WatchSource``. Expunging it
    means a stray RELATIONSHIP load raises ``DetachedInstanceError`` instead of
    quietly opening a second transaction while a remote listing is in flight.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm.exc import DetachedInstanceError

    tracker = scan_env
    source = _make_watch_source(db_session, normal_user, source_type="s3")

    plan = wst._load_scan_plan(source.id)

    assert tracker.depth == 0
    assert plan is not None
    assert plan["recursive"] is True
    for key, value in plan.items():
        if key == "client":
            continue
        assert not isinstance(value, WatchSource), key

    # The row the CLIENT is holding — the only one that outlives the scope.
    held = plan["client"].source
    assert held is not None, "the client never received the source row"
    assert sa_inspect(held).detached is True, (
        "the client is holding a session-attached WatchSource: a lazy load during "
        "the remote listing would silently open a second transaction"
    )
    # ...and the detachment is loud, not merely nominal.
    with pytest.raises(DetachedInstanceError):
        _ = held.email_links[0]

    # The claim it made still landed, despite the expunge.
    reloaded = db_session.query(WatchSource).filter(WatchSource.id == source.id).first()
    assert reloaded.last_scan_status == "running"


# --------------------------------------------------------------------------- #
# 9. cleanup_expired_files — MinIO + OpenSearch deletes, per expired file
# --------------------------------------------------------------------------- #
def test_cleanup_expired_files_purges_each_file_in_its_own_session(
    db_session, normal_user, monkeypatch
):
    """One transaction per file, not one across the whole retention pass."""
    import datetime as dt

    from app.models.media import FileStatus
    from app.services import file_cleanup_service as fcs
    from app.services import system_settings_service as sss
    from app.tasks import cleanup as cl

    tracker = _ScopeTracker(db_session)
    monkeypatch.setattr(cl, "session_scope", tracker.scope)
    monkeypatch.setattr(
        sss,
        "get_retention_config",
        lambda db: {
            "retention_enabled": True,
            "retention_days": 1,
            "delete_error_files": False,
            "timezone": "UTC",
            "run_time": "02:00",
            "last_run": None,
        },
    )
    monkeypatch.setattr(sss, "set_setting", lambda db, key, value, desc=None: None)

    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    files = []
    for _ in range(2):
        mf, _speakers = _make_transcribed_file(db_session, normal_user, speakers=1)
        mf.status = FileStatus.COMPLETED.value
        mf.completed_at = old
        mf.upload_time = old
        files.append(mf)
    db_session.flush()

    purged: list[int] = []

    def _purge(db, media_file):
        # MinIO object deletes + four OpenSearch deletes, per file.
        tracker.observe("auto_delete_media_file")
        purged.append(int(media_file.id))
        db.delete(media_file)
        return {"deleted": True, "file_uuid": str(media_file.uuid), "error": None}

    monkeypatch.setattr(fcs, "auto_delete_media_file", _purge)

    # The dev database this suite runs against holds unrelated aged files, and
    # the point of the test is the SESSION SHAPE, not the candidate query (that
    # has its own test below). Narrow the selection to the two rows this test
    # created — the real selector still runs, inside the real read scope.
    ours = {int(f.id) for f in files}
    real_select = cl._select_expired_files
    monkeypatch.setattr(
        cl,
        "_select_expired_files",
        lambda *a, **kw: [item for item in real_select(*a, **kw) if item[0] in ours],
    )

    result = cl.cleanup_expired_files(force=True)

    assert result == {"status": "completed", "deleted": 2, "failed": 0}, result
    assert sorted(purged) == sorted(int(f.id) for f in files)

    # The purge needs a session (it commits the row delete), so depth 1 is
    # correct here. What must NOT be shared is the scope: each file gets its
    # own, which shows up as a DIFFERENT scope count per call.
    depths = [d for label, d in tracker.observations if label == "auto_delete_media_file"]
    assert depths == [1, 1], tracker.observations
    counts = tracker.opened_at("auto_delete_media_file")
    assert len(set(counts)) == len(counts), (
        "both files were purged inside the SAME session — one transaction is "
        f"spanning the whole retention pass: {tracker.timeline}"
    )
    assert tracker.opened >= 4, f"expected read + 2 purges + write, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"


def test_cleanup_expired_selection_returns_plain_data(db_session, normal_user, monkeypatch):
    import datetime as dt

    from app.models.media import FileStatus
    from app.tasks import cleanup as cl

    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)
    media_file.status = FileStatus.COMPLETED.value
    media_file.completed_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    db_session.flush()

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    expired = cl._select_expired_files(
        db_session,
        {"delete_error_files": False},
        cutoff,
        cutoff,
        lambda org_id: None,
    )

    assert (media_file.id, str(media_file.uuid)) in expired
    for file_id, file_uuid in expired:
        assert isinstance(file_id, int)
        assert isinstance(file_uuid, str)


# --------------------------------------------------------------------------- #
# 10. transcription.embeddings v4 staging — OpenSearch search + write per profile
# --------------------------------------------------------------------------- #
def test_v4_profile_update_hits_opensearch_outside_the_session(
    db_session, normal_user, monkeypatch
):
    from app.models.media import SpeakerProfile
    from app.services import opensearch_service as oss

    tracker = _ScopeTracker(db_session)
    stored: list[dict] = []

    monkeypatch.setattr(temb, "session_scope", tracker.scope)

    media_file, speakers = _make_transcribed_file(db_session, normal_user, speakers=1)
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
    assert tracker.seen["opensearch_search"] == 0, _leak(tracker, "opensearch_search")
    assert tracker.seen["store_profile_embedding_v4"] == 0, _leak(
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


def test_scan_single_records_a_client_failure_as_a_scan_error(
    db_session, normal_user, scan_env, monkeypatch
):
    """A refused client must still leave the source with an ``error`` status.

    ``create_client`` moved into the read phase with the split, and it can refuse —
    unknown source type, a blocked private endpoint (SSRF guard), a missing optional
    dependency. Before the split that refusal happened inside ``_perform_scan``'s try
    block; if the split let it escape, the source would read ``running`` forever and
    ``scan_all`` would keep re-dispatching a scan that always dies the same way.
    """
    tracker = scan_env
    source = _make_watch_source(db_session, normal_user, source_type="s3")
    source_id = int(source.id)

    def _refuse(src):
        raise ValueError("The configured server address could not be used.")

    monkeypatch.setattr(wst, "create_client", _refuse)

    # ``_record_scan_result`` is observed, not replaced — it still runs and still
    # writes. The spy exists because the read phase's rollback unwinds the
    # savepoint this harness creates the fixture rows in, so the persisted row
    # cannot be read back here; what matters is that the error path REACHES the
    # writer with status "error" rather than escaping the task.
    recorded_calls: list[tuple] = []
    real_record = wst._record_scan_result

    def _spy(source_id_arg, summary_arg, started, duration, status, message):
        recorded_calls.append((status, message))
        return real_record(source_id_arg, summary_arg, started, duration, status, message)

    monkeypatch.setattr(wst, "_record_scan_result", _spy)

    summary = wst.scan_single.apply(args=[source_id]).get()

    assert summary["errors"] == 1, summary
    assert tracker.depth == 0, "a session scope survived the refused client"
    assert len(recorded_calls) == 1, recorded_calls
    status, message = recorded_calls[0]
    assert status == "error"
    assert "could not be used" in message


# --------------------------------------------------------------------------- #
# 13. ai.extract_topics — the SEVENTH LLM-in-transaction instance
#
#     Same shape as ai.generate_summary, and it could not be fixed from
#     app/tasks alone: ``TopicExtractionService`` was constructed with ``db=``
#     and both READ the transcript and WROTE the ``TopicSuggestion`` row through
#     it, so one ``session_scope`` in the task spanned the whole provider round
#     trip. The split therefore lives in the SERVICE, and this fixture patches
#     ``session_scope`` in BOTH modules so a scope opened at either end is seen.
# --------------------------------------------------------------------------- #
_TOPIC_LLM_JSON = """<thinking>ok</thinking>
<answer>
{"suggested_collections": [{"name": "Team Standups", "confidence": 0.9, "rationale": "r"}],
 "suggested_tags": [{"name": "budget", "confidence": 0.9, "rationale": "r"},
                    {"name": "hiring", "confidence": 0.8, "rationale": "r"}]}
</answer>"""


class _FakeTopicLLMService:
    """Stands in for the provider client. Records the prompt it was actually SENT."""

    user_context_window = 32768
    config = _FakeLLMConfig()

    def __init__(self, tracker, recorded, *, boom: bool = False):
        self._tracker = tracker
        self._recorded = recorded
        self._boom = boom

    def chat_completion(self, messages, **kwargs):
        # A multi-minute HTTP round trip against an external provider.
        self._tracker.observe("llm_chat_completion")
        self._recorded["messages"] = messages
        if self._boom:
            raise RuntimeError("provider returned 502")
        return SimpleNamespace(content=_TOPIC_LLM_JSON)


@pytest.fixture
def topic_extraction_env(db_session, monkeypatch):
    """Patch topic extraction's LLM/notification seams; keep the DB real."""
    from app.services import topic_extraction_service as tes
    from app.tasks import topic_extraction as tex

    tracker = _ScopeTracker(db_session)
    recorded: dict = {}

    # BOTH ends: the task's identity/masking reads and the service's read+write
    # phases. Patching only the task would let a service-side scope go unseen.
    monkeypatch.setattr(tex, "session_scope", tracker.scope)
    monkeypatch.setattr(tes, "session_scope", tracker.scope)
    monkeypatch.setattr(tex, "resolve_llm_masking", lambda db, media_file: None)
    monkeypatch.setattr(
        tex,
        "send_topic_extraction_notification",
        lambda **kw: recorded.setdefault("notifications", []).append(kw),
    )

    service = _FakeTopicLLMService(tracker, recorded)

    class _Factory:
        @staticmethod
        def create_from_settings(user_id=None):
            recorded["llm_user_id"] = user_id
            return service

    monkeypatch.setattr(tes, "LLMService", _Factory)

    tracker.recorded = recorded
    tracker.llm_service = service  # type: ignore[attr-defined]
    return tracker


def test_topic_extraction_calls_the_llm_outside_the_session(
    db_session, normal_user, topic_extraction_env
):
    """The regression: the provider round trip must run with zero scopes open."""
    from app.tasks import topic_extraction as tex

    tracker = topic_extraction_env
    media_file, _ = _make_transcribed_file(db_session, normal_user)

    result = tex.extract_topics_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "completed", result
    assert result["tag_count"] == 2
    assert result["collection_count"] == 1

    assert tracker.seen["llm_chat_completion"] == 0, _leak(tracker, "llm_chat_completion")
    assert tracker.opened >= 2, f"expected a read scope and a write scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
    assert tracker.depth == 0

    # The read phase really did produce the transcript the provider was sent, so
    # "zero scopes open" cannot be satisfied by sending nothing.
    prompt = tracker.recorded["messages"][-1]["content"]
    assert "hello" in prompt
    assert tracker.recorded["llm_user_id"] == normal_user.id

    # ...and the suggestion landed in Postgres.
    db_session.expire_all()
    stored = (
        db_session.query(TopicSuggestion)
        .filter(TopicSuggestion.media_file_id == media_file.id)
        .first()
    )
    assert stored is not None
    assert str(stored.uuid) == result["suggestion_id"]
    assert {t["name"] for t in stored.suggested_tags} == {"budget", "hiring"}


def test_topic_extraction_read_phase_returns_plain_data(
    db_session, normal_user, topic_extraction_env
):
    """``_load_extraction_inputs`` must not hand back live ORM instances."""
    from app.services.topic_extraction_service import TopicExtractionService

    tracker = topic_extraction_env
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    inputs = TopicExtractionService()._load_extraction_inputs(
        int(media_file.id), force_regenerate=False, redaction_cfg=None
    )

    assert tracker.depth == 0, "the read scope was still open on return"
    assert tracker.opened == 1
    assert inputs is not None
    assert inputs["user_id"] == normal_user.id
    assert isinstance(inputs["transcript"], str) and "hello there" in inputs["transcript"]
    for value in inputs.values():
        assert not isinstance(value, (MediaFile, Speaker, TranscriptSegment, TopicSuggestion))


def test_topic_extraction_releases_the_scope_when_the_provider_fails(
    db_session, normal_user, topic_extraction_env
):
    """A provider failure must not leave a scope open — and must report failure."""
    from app.tasks import topic_extraction as tex

    tracker = topic_extraction_env
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)
    tracker.llm_service._boom = True  # type: ignore[attr-defined]

    result = tex.extract_topics_task.apply(args=[str(media_file.uuid)]).get()

    assert result["status"] == "failed", result
    assert tracker.seen["llm_chat_completion"] == 0, _leak(tracker, "llm_chat_completion")
    assert tracker.depth == 0, "a session scope survived the failure"
    assert tracker.opened >= 1


def test_topic_extraction_defers_for_redaction_with_no_session_open(
    db_session, normal_user, topic_extraction_env, monkeypatch
):
    """``defer_for_redaction`` dispatches a Celery task — never inside a transaction."""
    from app.services.redaction.llm_guard import RedactionNotReadyError
    from app.tasks import topic_extraction as tex

    tracker = topic_extraction_env
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    def _not_ready(db, mf):
        raise RedactionNotReadyError("spans pending", retryable=True, file_id=int(mf.id))

    monkeypatch.setattr(tex, "resolve_llm_masking", _not_ready)

    deferred: list[int] = []

    def _defer(task, exc, **kw):
        deferred.append(tracker.depth)
        raise Retry()

    monkeypatch.setattr(tex, "defer_for_redaction", _defer)

    with pytest.raises(Retry):
        tex.extract_topics_task.apply(args=[str(media_file.uuid)], throw=True).get()

    assert deferred == [0], f"deferral ran with {deferred} scope(s) open"
    assert tracker.depth == 0
