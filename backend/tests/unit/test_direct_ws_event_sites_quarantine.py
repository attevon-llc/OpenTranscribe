"""The remaining 16 mechanical send_ws_event_for_file call sites (issue #908),
plus the two display-name leaks, the rename-propagation multi-file case, and
the three ``file_created`` sites.

Two test shapes, deliberately:

  * **Wiring tests** (postprocess, analytics, speaker_attribute_task,
    speaker_clustering, speaker_identification_task) — the wrapper's own
    suppression logic is already exhaustively covered by
    ``test_ws_event_file_wrapper.py``; re-deriving it at every call site would
    just re-test the same code 16 times. What actually varies per call site,
    and is worth pinning per site, is whether the RIGHT selector (and the
    right value) reaches the wrapper — a copy-paste that passed
    ``file_id=user_id`` by mistake would pass every other test in this repo.
  * **Real end-to-end quarantine tests** (the three ``file_created`` sites,
    the two display-name leaks, and the rename-propagation multi-file case)
    — these are the sites named explicitly in the issue as needing dedicated
    coverage, so they run the real suppression predicate against a real
    (savepoint-isolated) quarantined file, bridging sessions exactly like
    ``test_notification_quarantine.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Callable
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User


def _mk_user(db, *, admin: bool = False) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{'admin' if admin else 'user'}_wsdirect_{uid}@example.com",
        full_name="Direct WS site quarantine test user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=admin,
        role="super_admin" if admin else "user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, owner: User, quarantined: bool = False, **kwargs) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=kwargs.pop("filename", f"f_{str(fuuid)[:8]}.mp4"),
        storage_path="",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
        is_quarantined=quarantined,
        **kwargs,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def bridge_session(monkeypatch, db_session):
    """Bridge takedown_service's own session_scope to the test's savepoint session."""
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _yield_session(db_session))
    return db_session


def _capturing_send_ws_event(sink: list[tuple]) -> Callable[..., bool]:
    """A fake ``send_ws_event`` that records every call and reports success.

    A bare ``lambda *a, **k: sink.append(a) or True`` reads fine but mypy
    flags it (``list.append`` returns ``None``, and ``None or True`` still
    counts as "using" that return value) — a real function sidesteps it
    without reaching for ``# type: ignore``.
    """

    def _send(*args: object, **kwargs: object) -> bool:
        sink.append(args)
        return True

    return _send


# --------------------------------------------------------------------------- #
# Wiring tests: the right selector, with the right value, reaches the wrapper
# --------------------------------------------------------------------------- #


class TestPostprocessWiring:
    def test_finalize_transcription_enrichment_started_uses_file_id(self, monkeypatch):
        from app.tasks.transcription import postprocess

        captured = {}

        def _capture(user_id, ntype, data, **kw):
            captured["call"] = (user_id, ntype, data, kw)
            return True

        monkeypatch.setattr(postprocess, "send_ws_event_for_file", _capture)
        monkeypatch.setattr(postprocess, "send_progress_notification", lambda *a, **k: None)
        monkeypatch.setattr(postprocess, "send_completion_notification", lambda *a, **k: None)
        monkeypatch.setattr(postprocess, "update_task_status", lambda *a, **k: None)
        monkeypatch.setattr(postprocess, "enrich_and_dispatch", MagicMock())
        monkeypatch.setattr(
            postprocess,
            "session_scope",
            lambda: _yield_session(MagicMock()),
        )
        monkeypatch.setattr(postprocess, "_fire_completion_metering", lambda *a, **k: None)

        gpu_result = {
            "file_uuid": "uuid-42",
            "file_id": 42,
            "user_id": 7,
            "task_id": "task-1",
            "speaker_mapping": {},
            "native_embeddings": None,
            "use_native_embeddings": False,
            "asr_provider": "local",
            "downstream_tasks": None,
            "diarization_disabled": True,
        }

        postprocess.finalize_transcription.__wrapped__(gpu_result)

        user_id, ntype, data, kw = captured["call"]
        assert user_id == 7
        assert ntype == "enrichment_started"
        assert data["file_id"] == "uuid-42"
        assert kw == {"file_id": 42}

    def test_enrich_and_dispatch_search_indexing_uses_file_id(self, monkeypatch):
        from app.tasks.transcription import postprocess

        captured = {}

        def _capture(user_id, ntype, data, **kw):
            captured["call"] = (user_id, ntype, data, kw)
            return True

        monkeypatch.setattr(postprocess, "send_ws_event_for_file", _capture)
        monkeypatch.setattr(postprocess, "_index_transcript", lambda *a, **k: None)

        postprocess.enrich_and_dispatch(file_id=99, file_uuid="uuid-99", user_id=3)

        user_id, ntype, data, kw = captured["call"]
        assert user_id == 3
        assert ntype == "enrichment_task_complete"
        assert data == {"file_id": "uuid-99", "task": "search_indexing"}
        assert kw == {"file_id": 99}


class TestAnalyticsWiring:
    def test_analyze_transcript_task_uses_file_id(self, bridge_session, monkeypatch):
        from app.tasks import analytics as analytics_task

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner)

        captured = {}

        def _capture(user_id, ntype, data, **kw):
            captured["call"] = (user_id, ntype, data, kw)
            return True

        monkeypatch.setattr(analytics_task, "send_ws_event_for_file", _capture)
        monkeypatch.setattr(analytics_task, "session_scope", lambda: _yield_session(db))
        monkeypatch.setattr(
            analytics_task.AnalyticsService, "compute_and_save_analytics", lambda db, fid: True
        )

        analytics_task.analyze_transcript_task.push_request(id="task-analytics")
        try:
            analytics_task.analyze_transcript_task.run(str(file.uuid))
        finally:
            analytics_task.analyze_transcript_task.pop_request()

        user_id, ntype, data, kw = captured["call"]
        assert user_id == owner.id
        assert ntype == "enrichment_task_complete"
        assert data == {"file_id": str(file.uuid), "task": "analytics"}
        assert kw == {"file_id": file.id}


class TestSpeakerAttributeTaskWiring:
    def test_detect_speaker_attributes_uses_file_uuid_for_both_events(
        self, bridge_session, monkeypatch
    ):
        from app.models.media import Speaker
        from app.models.media import TranscriptSegment
        from app.tasks import speaker_attribute_task as sat

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner)
        speaker = Speaker(
            uuid=str(uuid_pkg.uuid4()),
            media_file_id=file.id,
            user_id=owner.id,
            name="SPEAKER_00",
        )
        db.add(speaker)
        db.flush()
        db.add(
            TranscriptSegment(
                uuid=str(uuid_pkg.uuid4()),
                media_file_id=file.id,
                speaker_id=speaker.id,
                start_time=0.0,
                end_time=4.0,
                text="hello",
            )
        )
        db.commit()

        calls = []

        def _capture(user_id, ntype, data, **kw):
            calls.append((user_id, ntype, data, kw))
            return True

        monkeypatch.setattr(sat, "send_ws_event_for_file", _capture)
        monkeypatch.setattr(sat, "session_scope", lambda: _yield_session(db))
        monkeypatch.setattr(sat, "_is_speaker_attribute_detection_enabled", lambda user_id: True)
        monkeypatch.setattr(
            "app.services.minio_service.minio_client",
            MagicMock(presigned_get_object=lambda **k: "http://minio.invalid/x.mp4"),
        )
        monkeypatch.setattr(
            "app.services.speaker_attribute_service.get_cached_attribute_service",
            lambda: MagicMock(load_models=lambda: None),
        )
        monkeypatch.setattr(sat, "_load_models_with_timeout", lambda *a, **k: None)
        monkeypatch.setattr(
            sat,
            "_run_gender_inference_parallel",
            lambda *a, **k: ({speaker.id: {"male": 0.9, "female": 0.1}}, {speaker.id: 1}),
        )
        monkeypatch.setattr(sat, "_dispatch_llm_speaker_identification", lambda file_uuid: None)

        sat._detect_speaker_attributes(str(file.uuid), owner.id, "task-attr")

        assert len(calls) == 2
        _, ntype_1, _, kw_1 = calls[0]
        assert ntype_1 == "speaker_updated"
        assert kw_1 == {"file_uuid": str(file.uuid)}
        _, ntype_2, _, kw_2 = calls[1]
        assert ntype_2 == "enrichment_task_complete"
        assert kw_2 == {"file_uuid": str(file.uuid)}


class TestSpeakerClusteringWiring:
    def test_cluster_speakers_for_file_uses_file_uuid_for_both_events(
        self, bridge_session, monkeypatch
    ):
        from app.tasks import speaker_clustering as sc

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner)

        calls = []

        def _capture(user_id, ntype, data, **kw):
            calls.append((user_id, ntype, data, kw))
            return True

        monkeypatch.setattr(sc, "send_ws_event_for_file", _capture)
        monkeypatch.setattr(sc, "session_scope", lambda: _yield_session(db))
        monkeypatch.setattr(
            "app.services.speaker_clustering_service.SpeakerClusteringService"
            ".cluster_speakers_for_file",
            lambda self, media_file_id, user_id: [1, 2],
        )

        sc.cluster_speakers_for_file.push_request(id="task-cluster")
        try:
            sc.cluster_speakers_for_file.run(str(file.uuid), owner.id)
        finally:
            sc.cluster_speakers_for_file.pop_request()

        assert len(calls) == 2
        _, ntype_1, data_1, kw_1 = calls[0]
        assert ntype_1 == "clustering_file_complete"
        assert kw_1 == {"file_uuid": str(file.uuid)}
        _, ntype_2, data_2, kw_2 = calls[1]
        assert ntype_2 == "enrichment_task_complete"
        assert kw_2 == {"file_uuid": str(file.uuid)}


class TestSpeakerIdentificationTaskWiring:
    def test_identify_speakers_llm_task_uses_file_id(self, monkeypatch):
        from app.tasks import speaker_identification_task as sid

        captured = {}

        def _capture(user_id, ntype, data, **kw):
            captured["call"] = (user_id, ntype, data, kw)
            return True

        monkeypatch.setattr(sid, "send_ws_event_for_file", _capture)
        monkeypatch.setattr(
            sid,
            "_load_identification_inputs",
            lambda file_uuid, task_id: {
                "file_id": 77,
                "user_id": 4,
                "full_transcript": "hello",
                "speaker_segments": {},
                "known_speakers": [],
                "metadata_context": "",
                "output_language": "en",
            },
        )
        monkeypatch.setattr(
            sid,
            "_generate_predictions",
            lambda *a, **k: {"speaker_predictions": [], "overall_confidence": "low"},
        )
        monkeypatch.setattr(sid, "_store_speaker_predictions", lambda *a, **k: None)
        monkeypatch.setattr(sid, "session_scope", lambda: _yield_session(MagicMock()))
        monkeypatch.setattr("app.utils.task_utils.update_task_status", lambda *a, **k: None)

        sid.identify_speakers_llm_task.push_request(id="task-ident")
        try:
            sid.identify_speakers_llm_task.run("uuid-77")
        finally:
            sid.identify_speakers_llm_task.pop_request()

        user_id, ntype, data, kw = captured["call"]
        assert user_id == 4
        assert ntype == "enrichment_task_complete"
        assert data == {"file_id": "uuid-77", "task": "speaker_identification"}
        assert kw == {"file_id": 77}


# --------------------------------------------------------------------------- #
# Real end-to-end quarantine tests: the three file_created sites
# --------------------------------------------------------------------------- #


class TestFileCreatedSites:
    def test_youtube_file_created_is_suppressed_for_a_quarantined_file(
        self, bridge_session, monkeypatch
    ):
        from app.tasks import youtube_processing as yt

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        yt._send_file_created_notification(owner.id, file)

        assert sent == []

    def test_youtube_file_created_is_delivered_for_a_clean_file(self, bridge_session, monkeypatch):
        from app.tasks import youtube_processing as yt

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        yt._send_file_created_notification(owner.id, file)

        assert len(sent) == 1
        assert sent[0][1] == "file_created"

    def test_url_processing_file_created_is_suppressed_for_a_quarantined_file(
        self, bridge_session, monkeypatch
    ):
        from app.api.endpoints.files import url_processing

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        url_processing._send_file_created_notification(file, owner.id)

        assert sent == []

    def test_url_processing_file_created_is_delivered_for_a_clean_file(
        self, bridge_session, monkeypatch
    ):
        from app.api.endpoints.files import url_processing

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        url_processing._send_file_created_notification(file, owner.id)

        assert len(sent) == 1
        assert sent[0][1] == "file_created"

    def test_watch_source_file_created_is_suppressed_for_a_quarantined_file(
        self, bridge_session, monkeypatch
    ):
        from app.services.watch_sources import processing as wsp

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        wsp._notify_file_created(owner.id, file)

        assert sent == []

    def test_watch_source_file_created_is_delivered_for_a_clean_file(
        self, bridge_session, monkeypatch
    ):
        from app.services.watch_sources import processing as wsp

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        wsp._notify_file_created(owner.id, file)

        assert len(sent) == 1
        assert sent[0][1] == "file_created"


# --------------------------------------------------------------------------- #
# Real end-to-end quarantine tests: the two display-name leaks
# --------------------------------------------------------------------------- #


class TestSpeakerMergeDisplayNameLeak:
    def test_speaker_merge_does_not_leak_a_display_name_for_a_quarantined_file(
        self, bridge_session, monkeypatch
    ):
        from app.tasks import speaker_merge_task as smt

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        from app.models.media import Speaker

        target = Speaker(
            uuid=str(uuid_pkg.uuid4()),
            media_file_id=file.id,
            user_id=owner.id,
            name="SPEAKER_00",
            display_name="Ada Lovelace",
        )
        db.add(target)
        db.commit()

        monkeypatch.setattr(
            "app.api.endpoints.speakers._clear_speaker_video_cache", lambda *a: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._merge_speaker_embeddings", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._update_opensearch_speaker_merge", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._update_profile_embeddings_after_merge",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._refresh_analytics_after_merge", lambda *a, **k: None
        )
        monkeypatch.setattr(smt, "session_scope", lambda: _yield_session(db))

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        smt.process_speaker_merge_background(
            source_speaker_uuid=str(uuid_pkg.uuid4()),
            target_speaker_uuid=str(target.uuid),
            user_id=owner.id,
            source_speaker_id=999,
            source_profile_id=None,
            target_profile_id=None,
            media_file_ids=[file.id],
        )

        assert sent == [], "the target speaker's display name must not leak for a quarantined file"

    def test_speaker_merge_delivers_the_display_name_for_a_clean_file(
        self, bridge_session, monkeypatch
    ):
        from app.tasks import speaker_merge_task as smt

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        from app.models.media import Speaker

        target = Speaker(
            uuid=str(uuid_pkg.uuid4()),
            media_file_id=file.id,
            user_id=owner.id,
            name="SPEAKER_00",
            display_name="Ada Lovelace",
        )
        db.add(target)
        db.commit()

        monkeypatch.setattr(
            "app.api.endpoints.speakers._clear_speaker_video_cache", lambda *a: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._merge_speaker_embeddings", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._update_opensearch_speaker_merge", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._update_profile_embeddings_after_merge",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._refresh_analytics_after_merge", lambda *a, **k: None
        )
        monkeypatch.setattr(smt, "session_scope", lambda: _yield_session(db))

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        smt.process_speaker_merge_background(
            source_speaker_uuid=str(uuid_pkg.uuid4()),
            target_speaker_uuid=str(target.uuid),
            user_id=owner.id,
            source_speaker_id=999,
            source_profile_id=None,
            target_profile_id=None,
            media_file_ids=[file.id],
        )

        assert len(sent) == 1
        user_id, ntype, data = sent[0]
        assert data["display_name"] == "Ada Lovelace"


class TestSpeakerUpdateDisplayNameLeak:
    def test_speaker_update_does_not_leak_a_display_name_for_a_quarantined_file(
        self, bridge_session, monkeypatch
    ):
        from app.tasks import speaker_update_task as sut

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        from app.models.media import Speaker

        speaker = Speaker(
            uuid=str(uuid_pkg.uuid4()),
            media_file_id=file.id,
            user_id=owner.id,
            name="SPEAKER_00",
            display_name="Grace Hopper",
        )
        db.add(speaker)
        db.commit()

        monkeypatch.setattr(
            "app.api.endpoints.speakers._handle_profile_embedding_updates", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._update_opensearch_speaker_name", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._push_speaker_profile_info", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._push_speaker_display_names", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._handle_speaker_labeling_workflow",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._load_profile_speaker_names", lambda *a, **k: []
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._clear_video_cache_for_speaker", lambda *a, **k: None
        )
        monkeypatch.setattr(sut, "session_scope", lambda: _yield_session(db))

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        sut.process_speaker_update_background(
            speaker_uuid=str(speaker.uuid),
            user_id=owner.id,
            display_name="Grace Hopper",
            speaker_id=speaker.id,
            old_profile_id=None,
            new_profile_id=None,
            was_auto_labeled=False,
            display_name_changed=False,
            media_file_id=file.id,
        )

        assert sent == [], "the speaker's display name must not leak for a quarantined file"

    def test_speaker_update_delivers_the_display_name_for_a_clean_file(
        self, bridge_session, monkeypatch
    ):
        from app.tasks import speaker_update_task as sut

        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        from app.models.media import Speaker

        speaker = Speaker(
            uuid=str(uuid_pkg.uuid4()),
            media_file_id=file.id,
            user_id=owner.id,
            name="SPEAKER_00",
            display_name="Grace Hopper",
        )
        db.add(speaker)
        db.commit()

        monkeypatch.setattr(
            "app.api.endpoints.speakers._handle_profile_embedding_updates", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._update_opensearch_speaker_name", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._push_speaker_profile_info", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._push_speaker_display_names", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._handle_speaker_labeling_workflow",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._load_profile_speaker_names", lambda *a, **k: []
        )
        monkeypatch.setattr(
            "app.api.endpoints.speakers._clear_video_cache_for_speaker", lambda *a, **k: None
        )
        monkeypatch.setattr(sut, "session_scope", lambda: _yield_session(db))

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        sut.process_speaker_update_background(
            speaker_uuid=str(speaker.uuid),
            user_id=owner.id,
            display_name="Grace Hopper",
            speaker_id=speaker.id,
            old_profile_id=None,
            new_profile_id=None,
            was_auto_labeled=False,
            display_name_changed=False,
            media_file_id=file.id,
        )

        assert len(sent) == 1
        user_id, ntype, data = sent[0]
        assert data["display_name"] == "Grace Hopper"


# --------------------------------------------------------------------------- #
# Real end-to-end quarantine test: the rename-propagation multi-file case
# --------------------------------------------------------------------------- #


class TestRenamePropagationMultiFile:
    def _run(self, db, file_uuids, monkeypatch):
        from app.tasks import rename_propagation_task as rpt

        monkeypatch.setattr(
            "app.tasks.search_indexing_task.extract_file_index_metadata",
            lambda db, media_file, file_id: {
                "title": "t",
                "tag_names": [],
                "upload_time": None,
                "language": "en",
                "content_type": "video/mp4",
                "duration": None,
                "file_size": 1000,
                "collection_ids": [],
                "accessible_user_ids": None,
                "organization_id": None,
            },
        )
        monkeypatch.setattr(
            "app.services.search.indexing_service.is_neural_pipeline_available", lambda: False
        )
        monkeypatch.setattr(
            "app.services.search.indexing_service.TranscriptIndexingService._index_digest_plane",
            lambda self, **kwargs: None,
        )

        sent: list[tuple] = []
        monkeypatch.setattr(
            "app.utils.websocket_notify.send_ws_event",
            _capturing_send_ws_event(sent),
        )

        result = rpt.regenerate_rename_digests.apply(
            kwargs={"file_uuids": file_uuids, "new_name": "New Name", "speaker_id": 1}
        ).get()
        return result, sent

    def test_drops_quarantined_uuids_and_recomputes_the_count(self, bridge_session, monkeypatch):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=True)
        f3 = _mk_file(db, owner=owner, quarantined=False)

        result, sent = self._run(db, [str(f1.uuid), str(f2.uuid), str(f3.uuid)], monkeypatch)

        assert result["regenerated"] == 3  # all 3 digests were regenerated
        assert len(sent) == 1
        user_id, ntype, data = sent[0]
        assert user_id == owner.id
        assert ntype == "speaker_rename_propagation"
        # The DISCLOSED payload only names the 2 files this recipient may see —
        # the quarantined f2 is dropped from BOTH the list and the count, so
        # the number itself cannot leak that a 3rd (hidden) file was touched.
        assert sorted(data["file_uuids"]) == sorted([str(f1.uuid), str(f3.uuid)])
        assert data["regenerated"] == 2

    def test_all_quarantined_sends_no_event_at_all(self, bridge_session, monkeypatch):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=True)
        f2 = _mk_file(db, owner=owner, quarantined=True)

        result, sent = self._run(db, [str(f1.uuid), str(f2.uuid)], monkeypatch)

        assert result["regenerated"] == 2  # the digests themselves were still regenerated
        assert sent == []

    def test_control_no_quarantine_delivers_the_full_list(self, bridge_session, monkeypatch):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=False)

        result, sent = self._run(db, [str(f1.uuid), str(f2.uuid)], monkeypatch)

        assert len(sent) == 1
        user_id, ntype, data = sent[0]
        assert sorted(data["file_uuids"]) == sorted([str(f1.uuid), str(f2.uuid)])
        assert data["regenerated"] == 2
