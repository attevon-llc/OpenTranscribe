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
===============================================  ====================================

The tests are behavioural, not structural: each swaps the module's
``session_scope`` for a depth-tracking stand-in over the savepointed
``db_session``, creates real rows, and has the stub for each slow call report the
open-scope depth *at the moment it runs*. Every test also asserts that at least
two scopes were opened, so a task that never touches the DB cannot pass.
"""

import contextlib
import uuid as uuid_mod
from typing import Any

import numpy as np
import pytest

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Task as TaskModel
from app.models.media import TranscriptSegment
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
# 2. download.prepare_media — PARTIAL: the residual hold is in the service
# --------------------------------------------------------------------------- #
def test_prepare_media_download_read_phase_is_outside_the_service_call(
    db_session, normal_user, monkeypatch
):
    """The task's own MediaFile read no longer shares a scope with the ffmpeg run.

    This is deliberately a weaker claim than the tests above.
    ``VideoProcessingService.extract_audio`` /
    ``process_video_with_subtitles`` take a ``Session`` and hold it across the
    MinIO download and the whole ffmpeg run, so *one* scope still spans the slow
    work — that part cannot be fixed from ``app/tasks``. What this asserts is
    what the task now controls: exactly one scope is open when the service is
    called (it is no longer nested inside the task's own read), and the read
    phase itself has closed.
    """
    tracker = _ScopeTracker(db_session)
    media_file, _ = _make_transcribed_file(db_session, normal_user, speakers=1)

    monkeypatch.setattr(mdl, "session_scope", tracker.scope)
    monkeypatch.setattr(mdl, "publish_download_event", lambda *a, **kw: None)
    monkeypatch.setattr(mdl, "release_download_prep_guard", lambda *a, **kw: None)
    monkeypatch.setattr(mdl, "MinIOService", lambda *a, **kw: object())

    class _FakeService:
        def __init__(self, minio):
            pass

        def extract_audio(self, *, db, file_id, original_object_name, audio_format):
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
    # The service call still runs inside a scope (that is the residual), but it
    # is its OWN scope: the task's read phase closed first, and nothing nests.
    assert tracker.seen["extract_audio"] == 1, tracker.observations
    assert tracker.seen["presign"] == 0, _leak(tracker, "presign")
    assert tracker.opened == 2, f"expected a read scope then a service scope, got {tracker.opened}"
    assert tracker.max_depth == 1, "session scopes must not nest"
