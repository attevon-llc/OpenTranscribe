"""``SpeakerRenameTracker`` — the after-commit half of issue #432.

The service-layer rename sites cannot dispatch inline: they rename many speakers
across many files inside a loop the caller commits, and they hold
``media_file_id``, not the UUID the chunk index is keyed by. The tracker is what
turns that into one ``update_by_query`` per file, after the commit.

The rules that matter, each of which is a way to lose data if it breaks:

* a rename to the name already indexed queues nothing (a no-op rewrite that would
  still bump the chat corpus version),
* renames coalesce per file (N tasks would each rewrite the same file-level
  ``speakers`` array and lose to the next on version conflict),
* renames to *different* names stay in different tasks (the batch-accept case:
  every speaker keeps its own suggestion),
* a rolled-back pass propagates nothing.

Real Postgres — the tracker resolves ``media_file_id`` -> ``uuid`` through the
session, and that query is where a wrong id silently produces ``None`` and a
dropped rename.
"""

import uuid as uuid_mod
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.services.speaker_rename_tracker import SpeakerRenameTracker

_DELAY = "app.tasks.rename_propagation_task.propagate_speaker_rename.delay"


def _media_file(db_session, user, name: str) -> MediaFile:
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


def _queued(delay_mock) -> dict[tuple[str, str], list[str]]:
    """``{(file_uuid, new_name): old_names}`` for every queued propagation."""
    return {
        (call.kwargs["file_uuid"], call.kwargs["new_name"]): sorted(call.kwargs["old_names"])
        for call in delay_mock.call_args_list
    }


@pytest.fixture
def tracker() -> SpeakerRenameTracker:
    return SpeakerRenameTracker()


class TestRecord:
    def test_a_rename_to_the_indexed_name_is_dropped(self, tracker):
        """Nothing to rewrite: queuing it would bump the corpus version for free."""
        tracker.record(7, "Dana", "Dana")
        assert tracker.pending == []

    def test_a_real_rename_is_kept(self, tracker):
        """Control for the drops above — the filter must not eat everything."""
        tracker.record(7, "SPEAKER_00", "Dana")
        assert tracker.pending == [(7, "SPEAKER_00", "Dana")]

    @pytest.mark.parametrize(
        "media_file_id,old,new",
        [
            (None, "SPEAKER_00", "Dana"),
            (7, None, "Dana"),
            (7, "", "Dana"),
            (7, "SPEAKER_00", None),
            (7, "SPEAKER_00", ""),
        ],
    )
    def test_incomplete_entries_are_dropped(self, tracker, media_file_id, old, new):
        tracker.record(media_file_id, old, new)
        assert tracker.pending == []


class TestFlush:
    def test_renames_in_one_file_become_one_task(self, db_session, normal_user, tracker):
        """Three diarized labels collapsing onto one person is ONE update_by_query."""
        media_file = _media_file(db_session, normal_user, "coalesce")

        for label in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_00"):
            tracker.record(int(media_file.id), label, "Dana")

        with patch(_DELAY) as delay_mock:
            queued = tracker.flush(db_session)

        assert queued == 1
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana"): ["SPEAKER_00", "SPEAKER_01"]}

    def test_the_same_person_across_files_becomes_one_task_per_file(
        self, db_session, normal_user, tracker
    ):
        """Cluster promotion reaches into every file the cluster spans."""
        first = _media_file(db_session, normal_user, "promo-a")
        second = _media_file(db_session, normal_user, "promo-b")

        tracker.record(int(first.id), "SPEAKER_00", "Dana")
        tracker.record(int(second.id), "SPEAKER_03", "Dana")

        with patch(_DELAY) as delay_mock:
            queued = tracker.flush(db_session)

        assert queued == 2
        assert _queued(delay_mock) == {
            (str(first.uuid), "Dana"): ["SPEAKER_00"],
            (str(second.uuid), "Dana"): ["SPEAKER_03"],
        }

    def test_different_new_names_are_never_merged(self, db_session, normal_user, tracker):
        """Batch accept applies each speaker's OWN suggestion, in the same file.

        Merging them would rename both speakers to whichever name won — the index
        would then disagree with Postgres about who said what.
        """
        media_file = _media_file(db_session, normal_user, "two-people")

        tracker.record(int(media_file.id), "SPEAKER_00", "Dana")
        tracker.record(int(media_file.id), "SPEAKER_01", "Ravi")

        with patch(_DELAY) as delay_mock:
            queued = tracker.flush(db_session)

        assert queued == 2
        assert _queued(delay_mock) == {
            (str(media_file.uuid), "Dana"): ["SPEAKER_00"],
            (str(media_file.uuid), "Ravi"): ["SPEAKER_01"],
        }

    def test_flush_clears_the_buffer(self, db_session, normal_user, tracker):
        """A second commit in the same pass must not re-queue the first one's work."""
        media_file = _media_file(db_session, normal_user, "clears")
        tracker.record(int(media_file.id), "SPEAKER_00", "Dana")

        with patch(_DELAY) as first_mock:
            tracker.flush(db_session)
        with patch(_DELAY) as second_mock:
            assert tracker.flush(db_session) == 0

        first_mock.assert_called_once()
        second_mock.assert_not_called()

    def test_an_unknown_file_id_queues_nothing(self, db_session, tracker):
        """A missing row resolves to no UUID — better nothing than a bad predicate."""
        tracker.record(2_000_000_000, "SPEAKER_00", "Dana")

        with patch(_DELAY) as delay_mock:
            assert tracker.flush(db_session) == 0

        delay_mock.assert_not_called()

    def test_a_failing_dispatch_does_not_raise_into_the_caller(
        self, db_session, normal_user, tracker
    ):
        """The rename is already committed; a dispatch failure must not undo that.

        The chunk plane stays stale until the next reindex — the pre-#405
        behaviour — and the caller still returns successfully.
        """
        media_file = _media_file(db_session, normal_user, "broker-down")
        tracker.record(int(media_file.id), "SPEAKER_00", "Dana")

        with patch(
            "app.tasks.rename_propagation_task.dispatch_speaker_rename",
            side_effect=OSError("broker unreachable"),
        ):
            assert tracker.flush(db_session) == 0

        assert tracker.pending == [], "a failed dispatch must not re-queue on the next flush"


class TestDiscard:
    def test_discard_drops_everything_recorded(self, db_session, normal_user, tracker):
        """A rolled-back pass must not leave the index holding a name Postgres never kept."""
        media_file = _media_file(db_session, normal_user, "rolled-back")
        tracker.record(int(media_file.id), "SPEAKER_00", "Dana")
        assert tracker.pending, "control: there was something to discard"

        tracker.discard()

        with patch(_DELAY) as delay_mock:
            assert tracker.flush(db_session) == 0
        delay_mock.assert_not_called()
