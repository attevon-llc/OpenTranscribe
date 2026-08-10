"""Issue #284 A2.6 — heavy speaker writes must not block the HTTP response.

``PUT /speakers/{uuid}`` (with ``profile_action="update_profile"``) and
``POST /speakers/{uuid}/merge/{target_uuid}`` used to fan out to OpenSearch, MinIO and
the analytics recomputation *inline*, so the request paid for a voiceprint average, a
consolidated profile-embedding recompute (one kNN read per profile member) and a video
cache clear before it could answer.

These tests pin the new contract:

* Postgres is still updated synchronously — the response body and the rows behind it
  are authoritative the moment the call returns.
* No OpenSearch / MinIO / analytics call happens on the request path.
* The deferred work is dispatched to the right Celery task with the arguments it needs
  to reproduce the old behaviour (notably the source speaker UUID, which cannot be
  re-read from Postgres once the merge has deleted the row).
"""

import uuid as uuid_mod
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment


def _make_media_file(db_session, user, name: str) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename=f"{name}.mp4",
        storage_path=f"test/{name}.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _make_speaker(db_session, user, media_file, name: str, **kwargs) -> Speaker:
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user.id,
        name=name,
        **kwargs,
    )
    db_session.add(speaker)
    db_session.flush()
    return speaker


@pytest.fixture
def no_opensearch():
    """Fail loudly if the request path touches OpenSearch, MinIO or analytics.

    Most of these are imported *inside* the helper that uses them, so patching the
    defining module is enough. ``update_speaker_display_name`` is the exception —
    ``endpoints/speakers.py`` imports it at module level, so it must be patched where
    it is looked up.
    """
    with (
        patch("app.api.endpoints.speakers.update_speaker_display_name") as name_mock,
        patch("app.services.opensearch_service.get_speaker_embedding") as get_mock,
        patch("app.services.opensearch_service.add_speaker_embedding") as add_mock,
        patch("app.services.opensearch_service.merge_speaker_embeddings") as merge_mock,
        patch("app.services.opensearch_service.update_speaker_profile") as profile_mock,
        patch(
            "app.services.profile_embedding_service.ProfileEmbeddingService.update_profile_embedding"
        ) as embed_mock,
        patch(
            "app.services.profile_embedding_service.ProfileEmbeddingService."
            "remove_speaker_from_profile_embedding"
        ) as remove_mock,
        patch(
            "app.services.analytics_service.AnalyticsService.refresh_analytics"
        ) as analytics_mock,
        patch(
            "app.services.video_processing_service.VideoProcessingService.clear_cache_for_media_file"
        ) as cache_mock,
    ):
        yield {
            "update_speaker_display_name": name_mock,
            "get_speaker_embedding": get_mock,
            "add_speaker_embedding": add_mock,
            "merge_speaker_embeddings": merge_mock,
            "update_speaker_profile": profile_mock,
            "update_profile_embedding": embed_mock,
            "remove_speaker_from_profile_embedding": remove_mock,
            "refresh_analytics": analytics_mock,
            "clear_cache_for_media_file": cache_mock,
        }


class TestProfileRenameOffloading:
    def test_rename_updates_postgres_and_defers_opensearch(
        self, client, db_session, normal_user, user_token_headers, no_opensearch
    ):
        """profile_action=update_profile renames in PG; OpenSearch fan-out is deferred."""
        profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Old Name"
        )
        db_session.add(profile)
        db_session.flush()

        media_file = _make_media_file(db_session, normal_user, "rename")
        edited = _make_speaker(
            db_session, normal_user, media_file, "SPEAKER_00", profile_id=profile.id
        )
        other_file = _make_media_file(db_session, normal_user, "rename-other")
        linked = _make_speaker(
            db_session,
            normal_user,
            other_file,
            "SPEAKER_01",
            profile_id=profile.id,
            display_name="Old Name",
        )

        headers = {"Authorization": user_token_headers["Authorization"]}
        with patch(
            "app.tasks.speaker_update_task.process_speaker_update_background.delay"
        ) as delay_mock:
            resp = client.put(
                f"/api/speakers/{edited.uuid}",
                json={"display_name": "New Name", "profile_action": "update_profile"},
                headers=headers,
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "New Name"

        # Postgres is authoritative immediately: profile and every linked speaker renamed.
        db_session.refresh(profile)
        db_session.refresh(linked)
        assert profile.name == "New Name"
        assert linked.display_name == "New Name"

        # Nothing heavy ran on the request path.
        no_opensearch["update_speaker_display_name"].assert_not_called()
        no_opensearch["update_profile_embedding"].assert_not_called()

        # The rename is handed to the background task so it can replay it.
        delay_mock.assert_called_once()
        kwargs = delay_mock.call_args.kwargs
        assert kwargs["renamed_profile_id"] == profile.id
        assert kwargs["display_name"] == "New Name"
        assert kwargs["speaker_uuid"] == str(edited.uuid)

    def test_plain_rename_does_not_flag_a_profile_rename(
        self, client, db_session, normal_user, user_token_headers, no_opensearch
    ):
        """Without profile_action there is no profile to replay."""
        media_file = _make_media_file(db_session, normal_user, "plain")
        speaker = _make_speaker(db_session, normal_user, media_file, "SPEAKER_00")

        headers = {"Authorization": user_token_headers["Authorization"]}
        with patch(
            "app.tasks.speaker_update_task.process_speaker_update_background.delay"
        ) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"display_name": "Alice"},
                headers=headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["renamed_profile_id"] is None

    def test_background_task_replays_the_rename(self, db_session, normal_user, no_opensearch):
        """The deferred helper pushes every linked speaker's new name to OpenSearch."""
        from app.api.endpoints.speakers import _sync_profile_rename_to_opensearch

        profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Bob")
        db_session.add(profile)
        db_session.flush()

        media_file = _make_media_file(db_session, normal_user, "replay")
        first = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_00",
            profile_id=profile.id,
            display_name="Bob",
        )
        second = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_01",
            profile_id=profile.id,
            display_name="Bob",
        )

        _sync_profile_rename_to_opensearch(db_session, profile.id)

        pushed = {
            call.args[0] for call in no_opensearch["update_speaker_display_name"].call_args_list
        }
        assert pushed == {str(first.uuid), str(second.uuid)}


class TestMergeOffloading:
    def test_merge_commits_postgres_and_dispatches_the_task(
        self, client, db_session, normal_user, user_token_headers, no_opensearch
    ):
        source_file = _make_media_file(db_session, normal_user, "merge-source")
        target_file = _make_media_file(db_session, normal_user, "merge-target")

        source_profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Source Profile"
        )
        target_profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Target Profile"
        )
        db_session.add_all([source_profile, target_profile])
        db_session.flush()

        source = _make_speaker(
            db_session, normal_user, source_file, "SPEAKER_00", profile_id=source_profile.id
        )
        target = _make_speaker(
            db_session, normal_user, target_file, "SPEAKER_01", profile_id=target_profile.id
        )

        segment = TranscriptSegment(
            uuid=str(uuid_mod.uuid4()),
            media_file_id=source_file.id,
            speaker_id=source.id,
            start_time=0.0,
            end_time=1.0,
            text="hello",
        )
        db_session.add(segment)
        db_session.flush()

        source_uuid = str(source.uuid)
        source_id = source.id

        headers = {"Authorization": user_token_headers["Authorization"]}
        with patch(
            "app.tasks.speaker_merge_task.process_speaker_merge_background.delay"
        ) as delay_mock:
            resp = client.post(f"/api/speakers/{source_uuid}/merge/{target.uuid}", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["uuid"] == str(target.uuid)

        # Postgres side is complete before the response.
        db_session.refresh(segment)
        assert segment.speaker_id == target.id
        assert db_session.query(Speaker).filter(Speaker.id == source_id).first() is None

        # Nothing heavy ran on the request path.
        for name in (
            "get_speaker_embedding",
            "add_speaker_embedding",
            "merge_speaker_embeddings",
            "update_profile_embedding",
            "remove_speaker_from_profile_embedding",
            "refresh_analytics",
            "clear_cache_for_media_file",
        ):
            no_opensearch[name].assert_not_called()

        delay_mock.assert_called_once()
        kwargs = delay_mock.call_args.kwargs
        # The source UUID must be captured before the delete — it cannot be re-read.
        assert kwargs["source_speaker_uuid"] == source_uuid
        assert kwargs["target_speaker_uuid"] == str(target.uuid)
        assert kwargs["source_speaker_id"] == source_id
        assert kwargs["source_profile_id"] == source_profile.id
        assert kwargs["target_profile_id"] == target_profile.id
        assert set(kwargs["media_file_ids"]) == {source_file.id, target_file.id}

    def test_merge_task_arguments_are_json_serialisable(self):
        """Celery kwargs must survive the JSON broker serializer."""
        import json

        from app.tasks.speaker_merge_task import process_speaker_merge_background

        assert process_speaker_merge_background.name == "process_speaker_merge_background"
        json.dumps(
            {
                "source_speaker_uuid": str(uuid_mod.uuid4()),
                "target_speaker_uuid": str(uuid_mod.uuid4()),
                "user_id": 1,
                "source_speaker_id": 2,
                "source_profile_id": None,
                "target_profile_id": 3,
                "media_file_ids": [4, 5],
            }
        )

    def test_merge_task_is_routed_to_the_cpu_queue(self):
        """A task with no task_routes entry raises at dispatch (task_create_missing_queues)."""
        from app.core.celery import celery_app

        routes = celery_app.conf.task_routes
        assert "process_speaker_merge_background" in routes
        assert "app.tasks.speaker_merge_task" in celery_app.conf.include
