"""Every rename path must hand the chunk plane its stale name (issue #405).

The rewrite itself is covered against a real cluster in
``tests/integration/test_rename_propagation_chunks.py``. These tests cover the
half that a cluster cannot see: whether each production rename path *dispatches*
the propagation, and whether it passes the name the chunks were actually indexed
with.

That last part is the subtle one. Chunks carry ``display_name or name``, and by
the time any background worker runs, Postgres holds only the NEW name — so a
path that forgets to capture the old value before overwriting it has nothing to
match on and propagates nothing, silently.
"""

import uuid as uuid_mod
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile

_DELAY = "app.tasks.rename_propagation_task.propagate_speaker_rename.delay"
_TITLE_DELAY = "app.tasks.rename_propagation_task.propagate_title_rename.delay"


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
def quiet_opensearch():
    """Keep the request path off OpenSearch without hiding the dispatch under test."""
    with (
        patch("app.api.endpoints.speakers.update_speaker_display_name"),
        patch("app.services.opensearch_service.update_speaker_profile"),
    ):
        yield


def _queued(delay_mock) -> dict[str, list[str]]:
    """``{file_uuid: old_names}`` for every queued propagation."""
    return {
        call.kwargs["file_uuid"]: sorted(call.kwargs["old_names"])
        for call in delay_mock.call_args_list
    }


class TestSpeakerRenameEndpoint:
    def test_rename_queues_propagation_with_the_diarizer_label(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """An unlabelled speaker's chunks carry ``name`` — that is the string to match."""
        media_file = _make_media_file(db_session, normal_user, "first-label")
        speaker = _make_speaker(db_session, normal_user, media_file, "SPEAKER_00")

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"display_name": "Dana"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs == {
            "file_uuid": str(media_file.uuid),
            "old_names": ["SPEAKER_00"],
            "new_name": "Dana",
        }

    def test_relabel_queues_the_previous_display_name_not_the_raw_label(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """Once labelled, chunks carry the display name — matching ``name`` finds nothing."""
        media_file = _make_media_file(db_session, normal_user, "relabel")
        speaker = _make_speaker(
            db_session, normal_user, media_file, "SPEAKER_00", display_name="Dana"
        )

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"display_name": "Dana Whitfield"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        assert delay_mock.call_args.kwargs["old_names"] == ["Dana"]
        assert delay_mock.call_args.kwargs["new_name"] == "Dana Whitfield"

    def test_renaming_to_the_same_name_queues_nothing(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """No rewrite means no work and no cache invalidation."""
        media_file = _make_media_file(db_session, normal_user, "no-op")
        speaker = _make_speaker(
            db_session, normal_user, media_file, "SPEAKER_00", display_name="Dana"
        )

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"display_name": "Dana"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_not_called()

    def test_profile_rename_queues_every_file_the_profile_reaches(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """A profile rename is cross-file — each affected file needs its own rewrite."""
        profile = SpeakerProfile(
            uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Old Name"
        )
        db_session.add(profile)
        db_session.flush()

        edited_file = _make_media_file(db_session, normal_user, "profile-edited")
        edited = _make_speaker(
            db_session,
            normal_user,
            edited_file,
            "SPEAKER_00",
            profile_id=profile.id,
            display_name="Old Name",
        )
        other_file = _make_media_file(db_session, normal_user, "profile-other")
        _make_speaker(
            db_session,
            normal_user,
            other_file,
            "SPEAKER_01",
            profile_id=profile.id,
            display_name="Old Name",
        )

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{edited.uuid}",
                json={"display_name": "New Name", "profile_action": "update_profile"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        assert _queued(delay_mock) == {
            str(edited_file.uuid): ["Old Name"],
            str(other_file.uuid): ["Old Name"],
        }
        assert {c.kwargs["new_name"] for c in delay_mock.call_args_list} == {"New Name"}


class TestProfileRenameCapture:
    def test_the_pre_rename_names_are_returned_before_the_overwrite(self, db_session, normal_user):
        """``_handle_update_profile_action`` is the last place the old names exist."""
        from app.api.endpoints.speakers import _handle_update_profile_action

        profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Bob")
        db_session.add(profile)
        db_session.flush()

        labelled_file = _make_media_file(db_session, normal_user, "capture-labelled")
        _make_speaker(
            db_session,
            normal_user,
            labelled_file,
            "SPEAKER_00",
            profile_id=profile.id,
            display_name="Bob",
        )
        unlabelled_file = _make_media_file(db_session, normal_user, "capture-unlabelled")
        unlabelled = _make_speaker(
            db_session, normal_user, unlabelled_file, "SPEAKER_07", profile_id=profile.id
        )

        renames = _handle_update_profile_action(profile.id, "Robert", normal_user, db_session)

        assert renames is not None, "the profile exists, so the rename list must not be None"
        assert sorted(renames) == sorted(
            [(str(labelled_file.uuid), "Bob"), (str(unlabelled_file.uuid), "SPEAKER_07")]
        )
        db_session.flush()
        assert unlabelled.display_name == "Robert", "Postgres is still updated in place"

    def test_a_missing_profile_is_distinguishable_from_one_with_no_speakers(
        self, db_session, normal_user
    ):
        """``None`` means "no such profile"; ``[]`` means "renamed, nothing to replay"."""
        from app.api.endpoints.speakers import _handle_update_profile_action

        empty = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Empty")
        db_session.add(empty)
        db_session.flush()

        assert _handle_update_profile_action(empty.id, "Renamed", normal_user, db_session) == []
        assert _handle_update_profile_action(-1, "Renamed", normal_user, db_session) is None


class TestRetroactiveAutoApply:
    def test_auto_applied_match_reports_the_stale_chunk_name(
        self, db_session, normal_user, quiet_opensearch
    ):
        """The batch path renames other files' speakers; each one leaves stale chunks."""
        from app.api.endpoints.speaker_update import _apply_high_confidence_match

        source_file = _make_media_file(db_session, normal_user, "auto-source")
        target_file = _make_media_file(db_session, normal_user, "auto-target")
        labelled = _make_speaker(
            db_session, normal_user, source_file, "SPEAKER_00", display_name="Dana"
        )
        # ``_process_speaker_match`` stamps the similarity before calling this.
        matched = _make_speaker(db_session, normal_user, target_file, "SPEAKER_03", confidence=0.91)

        # `trigger` is plain data, never an ORM instance: the session-lifetime rule
        # (backend/app/tasks/CLAUDE.md) requires the read phase to hand on values,
        # so an attribute read after the scope closes cannot silently reopen a
        # transaction. Passing `labelled` itself here is what the pre-merge
        # signature did, and it is the thing that rule exists to stop.
        trigger = {
            "id": labelled.id,
            "display_name": labelled.display_name,
            "profile_id": labelled.profile_id,
        }
        _, rename = _apply_high_confidence_match(db_session, matched, trigger)

        assert rename == (str(target_file.uuid), "SPEAKER_03")
        assert matched.display_name == "Dana"


class TestTitleRename:
    def test_title_change_queues_the_chunk_rewrite(self, db_session, normal_user):
        """``update_transcript_title`` only reaches the full-document index."""
        from app.api.endpoints.files.crud import update_media_file
        from app.schemas.media import MediaFileUpdate

        media_file = _make_media_file(db_session, normal_user, "titled")
        media_file.title = "Old title"
        db_session.flush()

        with (
            patch("app.api.endpoints.files.crud.update_transcript_title") as full_doc_mock,
            patch(_TITLE_DELAY) as delay_mock,
        ):
            update_media_file(
                db_session,
                str(media_file.uuid),
                MediaFileUpdate(title="New title"),
                normal_user,
            )

        full_doc_mock.assert_called_once_with(str(media_file.uuid), "New title")
        delay_mock.assert_called_once_with(file_uuid=str(media_file.uuid), new_title="New title")

        # Mock bookkeeping alone would pass even if the rename never reached
        # Postgres — the queued rewrite would then propagate a title the
        # database does not hold. Assert the durable outcome too.
        db_session.refresh(media_file)
        assert media_file.title == "New title"

    def test_an_unchanged_title_queues_nothing(self, db_session, normal_user):
        from app.api.endpoints.files.crud import update_media_file
        from app.schemas.media import MediaFileUpdate

        media_file = _make_media_file(db_session, normal_user, "same-title")
        media_file.title = "Same title"
        db_session.flush()

        with (
            patch("app.api.endpoints.files.crud.update_transcript_title"),
            patch(_TITLE_DELAY) as delay_mock,
        ):
            update_media_file(
                db_session,
                str(media_file.uuid),
                MediaFileUpdate(title="Same title"),
                normal_user,
            )

        delay_mock.assert_not_called()

        # ...and the no-op stayed a no-op: asserting only that nothing was
        # queued would also pass if the update had silently cleared the title.
        db_session.refresh(media_file)
        assert media_file.title == "Same title"


class TestDispatchHelper:
    def test_renames_are_coalesced_into_one_task_per_file(self):
        """Four labels collapsing onto one person in one file is ONE update_by_query.

        Four separate tasks would each rewrite the same file-level ``speakers``
        array and lose to the next on version conflict.
        """
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_a, file_b = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        with patch(_DELAY) as delay_mock:
            queued = dispatch_speaker_rename(
                [
                    (file_a, "SPEAKER_00"),
                    (file_a, "SPEAKER_01"),
                    (file_a, "SPEAKER_00"),
                    (file_b, "SPEAKER_02"),
                ],
                "Dana",
            )

        assert queued == 2
        assert _queued(delay_mock) == {
            file_a: ["SPEAKER_00", "SPEAKER_01"],
            file_b: ["SPEAKER_02"],
        }

    def test_incomplete_and_already_current_entries_are_dropped(self):
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        file_uuid = str(uuid_mod.uuid4())
        with patch(_DELAY) as delay_mock:
            queued = dispatch_speaker_rename(
                [(file_uuid, "Dana"), (None, "SPEAKER_00"), (file_uuid, None), (file_uuid, "")],
                "Dana",
            )

        assert queued == 0
        delay_mock.assert_not_called()


class TestTaskWiring:
    def test_tasks_are_registered_and_routed(self):
        """A task with no ``task_routes`` entry raises at dispatch."""
        from app.core.celery import celery_app

        assert "app.tasks.rename_propagation_task" in celery_app.conf.include
        for name in ("propagate_speaker_rename", "propagate_title_rename"):
            assert name in celery_app.conf.task_routes
            assert celery_app.conf.task_routes[name] == {"queue": "cpu"}

    def test_task_arguments_survive_the_json_broker_serializer(self):
        import json

        from app.tasks.rename_propagation_task import propagate_speaker_rename
        from app.tasks.rename_propagation_task import propagate_title_rename

        assert propagate_speaker_rename.name == "propagate_speaker_rename"
        assert propagate_title_rename.name == "propagate_title_rename"
        json.dumps(
            {
                "file_uuid": str(uuid_mod.uuid4()),
                "old_names": ["SPEAKER_00"],
                "new_name": "Dana",
            }
        )
