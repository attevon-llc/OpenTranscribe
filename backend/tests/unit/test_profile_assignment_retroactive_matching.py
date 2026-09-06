"""Assigning a speaker to a profile must re-score the rest of the library.

A `SpeakerProfile` has no voiceprint of its own until a speaker is attached to
it — that assignment is what gives it embeddings. So the moment a profile
becomes matchable is precisely the moment the assignment commits.

Nothing re-scored at that moment. `trigger_retroactive_matching` — which
compares a speaker's voiceprint against every other speaker, auto-applies at
>=75% and suggests at 50-74% — was reachable from exactly one place:
`process_speaker_update_background`, behind
`if display_name_changed and display_name and display_name.strip()`. Renaming a
speaker ran it; assigning one to a profile did not, and
`POST /speakers/{uuid}/assign-profile` never queued that task at all.

The user-visible symptom: a profile is created, one cluster is assigned to it,
and every other recording of the same voice stays unmatched with an empty
Inbox — including the other clusters of that same voice in the same file, which
is the case that made it look like the feature simply did not work.
"""

import contextlib
import uuid
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile


@pytest.fixture(autouse=True)
def _use_test_session(db_session):
    """Point the task's own ``session_scope`` at the test session.

    ``_rescore_against_profile`` deliberately opens its own short session (it
    runs inside a Celery task, outside any request scope). Under this suite's
    savepoint isolation a separate session cannot see the uncommitted rows the
    test just created, so it would log "speaker disappeared" and assert
    nothing — the documented harness trap in ``backend/tests/CLAUDE.md``.
    """

    @contextlib.contextmanager
    def _scope():
        yield db_session

    with patch("app.tasks.speaker_update_task.session_scope", _scope):
        yield


@pytest.fixture
def file_with_speaker(db_session, normal_user):
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=normal_user.id,
        filename="assign_match.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()

    speaker = Speaker(
        uuid=str(uuid.uuid4()),
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="",
    )
    db_session.add(speaker)
    db_session.commit()
    return media_file, speaker


@pytest.fixture
def profile(db_session, normal_user):
    p = SpeakerProfile(
        uuid=str(uuid.uuid4()),
        user_id=normal_user.id,
        name=f"Profile {uuid.uuid4().hex[:8]}",
    )
    db_session.add(p)
    db_session.commit()
    return p


class TestProfileAssignmentTriggersMatching:
    def test_the_task_runs_matching_when_a_profile_is_newly_attached(
        self, db_session, normal_user, file_with_speaker, profile
    ):
        """The gate used to be rename-only, so this path scored nothing."""
        from app.tasks import speaker_update_task

        _, speaker = file_with_speaker
        speaker.profile_id = profile.id
        db_session.commit()

        called: list[int] = []

        def fake_matching(updated_speaker, db):
            called.append(int(updated_speaker.id))
            return {"auto_applied_count": 0, "suggested_count": 2}

        assert speaker_update_task._should_rescore_after_profile_change(
            display_name_changed=False,
            display_name="",
            old_profile_id=None,
            new_profile_id=profile.id,
        ), "a newly attached profile must re-score"

        with patch(
            "app.api.endpoints.speaker_update.trigger_retroactive_matching",
            side_effect=fake_matching,
        ):
            result = speaker_update_task._rescore_against_profile(int(speaker.id))

        assert called == [int(speaker.id)]
        assert result["suggested_count"] == 2

    def test_a_rename_is_still_handled_by_the_labeling_workflow(self):
        """The control: the rename path must not be diverted into the new one.

        Without this, moving every case onto the profile branch would satisfy
        the assertion above while silently dropping `auto_create_or_assign_profile`,
        which the rename path needs and the assignment path must not re-run.
        """
        from app.tasks import speaker_update_task

        assert not speaker_update_task._should_rescore_after_profile_change(
            display_name_changed=True,
            display_name="Ada Lovelace",
            old_profile_id=None,
            new_profile_id=7,
        )

    def test_detaching_a_profile_scores_nothing(self):
        """Removing a speaker from a profile gives no profile a new voiceprint."""
        from app.tasks import speaker_update_task

        assert not speaker_update_task._should_rescore_after_profile_change(
            display_name_changed=False,
            display_name="",
            old_profile_id=7,
            new_profile_id=None,
        )

    def test_an_unchanged_profile_scores_nothing(self):
        """A no-op update must not queue a full-library similarity pass."""
        from app.tasks import speaker_update_task

        assert not speaker_update_task._should_rescore_after_profile_change(
            display_name_changed=False,
            display_name="",
            old_profile_id=7,
            new_profile_id=7,
        )

    def test_the_assign_endpoint_queues_the_background_task(
        self, db_session, normal_user, file_with_speaker, profile
    ):
        """The endpoint never queued it at all, so the gate above was unreachable."""
        from app.api.endpoints import speaker_profiles

        _, speaker = file_with_speaker
        dispatched: list[dict] = []

        with patch.object(
            speaker_profiles.process_speaker_update_background,
            "delay",
            side_effect=lambda **kw: dispatched.append(kw),
        ):
            speaker_profiles._queue_retroactive_matching_after_assignment(
                speaker_uuid=str(speaker.uuid),
                user_id=int(normal_user.id),
                speaker_id=int(speaker.id),
                old_profile_id=None,
                new_profile_id=int(profile.id),
                media_file_id=int(speaker.media_file_id),
            )

        assert len(dispatched) == 1
        kw = dispatched[0]
        assert kw["new_profile_id"] == int(profile.id)
        assert kw["old_profile_id"] is None
        # Critically NOT a rename: setting this would send the assignment down
        # the labeling workflow, which re-runs profile auto-creation.
        assert kw["display_name_changed"] is False
