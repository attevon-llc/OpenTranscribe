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
_DIGEST_DELAY = "app.tasks.rename_propagation_task.regenerate_rename_digests.delay"


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


@pytest.fixture
def quiet_rename_dispatch():
    """Capture the chunk-plane dispatch and silence the digest-plane one beside it.

    ``dispatch_speaker_rename`` queues two things per coalesced file: the
    ``propagate_speaker_rename`` rewrite (what these tests assert on) and a
    ``regenerate_rename_digests`` batch. Only the first is yielded — patching the
    second keeps the digest fan-out from reaching a broker without hiding the
    dispatch under test.
    """
    with patch(_DIGEST_DELAY), patch(_DELAY) as delay_mock:
        yield delay_mock


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
        # `speaker_id` is carried so the task can re-resolve the current name at
        # run time and converge when two renames race (see the task module).
        assert delay_mock.call_args.kwargs == {
            "file_uuid": str(media_file.uuid),
            "old_names": ["SPEAKER_00"],
            "new_name": "Dana",
            "speaker_id": speaker.id,
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

    def test_clearing_a_display_name_queues_the_revert_to_the_raw_label(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """``{"display_name": ""}`` is how a user undoes a label, and it must propagate.

        ``display_name`` is ``str | None`` and is in ``SPEAKER_UPDATABLE_FIELDS``,
        so an empty string is a legal request. Postgres reverts to the diarizer
        label and a reindex would write ``SPEAKER_00`` — but dispatch keyed off
        the truthiness of ``display_name``, so the chunk plane kept "Dana"
        forever and the search facet went on offering a name that existed
        nowhere else.
        """
        media_file = _make_media_file(db_session, normal_user, "cleared-label")
        speaker = _make_speaker(
            db_session, normal_user, media_file, "SPEAKER_00", display_name="Dana"
        )

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"display_name": ""},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["old_names"] == ["Dana"]
        assert delay_mock.call_args.kwargs["new_name"] == "SPEAKER_00", (
            "clearing the label must propagate the value a reindex would write "
            "(display_name or name), not skip the rewrite"
        )

    def test_relabelling_over_a_confident_suggestion_queues_the_suggestion_not_the_raw_label(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """Issue #605, the exact shape of speaker 74070: a confident LLM/embedding
        suggestion with NO ``display_name`` set is what the chunk plane was
        indexed with (``canonical_speaker_label``, ``confidence >= 0.75``). The
        old ``display_name or name`` capture ignored ``suggested_name`` entirely,
        so ``old_names`` computed the raw diarizer label — a filter the
        ``update_by_query`` never matched, silently leaving the drift in place.
        """
        media_file = _make_media_file(db_session, normal_user, "suggestion-relabel")
        speaker = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_01",
            suggested_name="Joe Rogan (Host)",
            confidence=0.9,
            suggestion_source="llm_analysis",
        )

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"display_name": "Joe Rogan"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["old_names"] == ["Joe Rogan (Host)"]
        assert delay_mock.call_args.kwargs["new_name"] == "Joe Rogan"

    def test_editing_name_alone_queues_propagation_for_an_unlabelled_speaker(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """``name`` is updatable, and for an unlabelled speaker it IS the indexed value.

        The indexer writes ``display_name or name``, so editing ``name`` on a
        speaker with no display name changes what the chunks should carry — but
        dispatch keyed solely off ``display_name`` and propagated nothing.
        """
        media_file = _make_media_file(db_session, normal_user, "name-only")
        speaker = _make_speaker(db_session, normal_user, media_file, "SPEAKER_00")

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"name": "SPEAKER_07"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["old_names"] == ["SPEAKER_00"]
        assert delay_mock.call_args.kwargs["new_name"] == "SPEAKER_07"

    def test_editing_name_under_a_display_name_queues_nothing(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """The control: with a display name set, ``name`` is not what was indexed.

        Without this, the two tests above would also pass if dispatch fired on
        every update regardless of whether the indexed value moved.
        """
        media_file = _make_media_file(db_session, normal_user, "name-under-label")
        speaker = _make_speaker(
            db_session, normal_user, media_file, "SPEAKER_00", display_name="Dana"
        )

        with patch(_DELAY) as delay_mock:
            resp = client.put(
                f"/api/speakers/{speaker.uuid}",
                json={"name": "SPEAKER_07"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_not_called()

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


class TestSpeakerProfileUpdateEndpoint:
    """``PUT /speaker-profiles/profiles/{uuid}`` — the second profile-rename path (#675).

    There are two ways to rename a profile and only one of them was covered.
    ``PUT /speakers/{uuid}`` with ``profile_action="update_profile"`` (the file
    detail page) has dispatched since #405; ``PUT /speaker-profiles/profiles/{uuid}``
    — what the **Speakers page's** profile editor calls
    (``frontend/src/routes/speakers/+page.svelte``) — set ``SpeakerProfile.name``
    and nothing else.

    ⚠️ **``SpeakerProfile.name`` is not a field of ``transcript_chunks``.** The
    chunk plane carries ``canonical_speaker_label_for_row(speaker)``, resolved from
    ``Speaker.display_name`` / ``suggested_name`` / ``name`` — a profile is not
    consulted at index time at all. So dispatching a propagation from this endpoint
    *without* also re-applying the profile name to its member speakers would write a
    name into the index that Postgres does not hold, and the next reindex would
    revert it. The rename and the dispatch are one unit, which is why these tests
    assert on both halves.
    """

    def test_renaming_a_profile_queues_propagation_for_every_file_it_reaches(
        self, client, db_session, normal_user, user_token_headers, quiet_rename_dispatch
    ):
        """One profile, three files, three different indexed labels.

        The old name is per-SPEAKER, not per-profile: a member indexed under a
        confident suggestion and a member with no label at all were indexed under
        strings the profile never held, so a dispatch keyed on the profile's own
        previous name would match nothing for either.
        """
        profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Bob")
        db_session.add(profile)
        db_session.flush()

        labelled_file = _make_media_file(db_session, normal_user, "profile-put-labelled")
        _make_speaker(
            db_session,
            normal_user,
            labelled_file,
            "SPEAKER_00",
            profile_id=profile.id,
            display_name="Bob",
        )
        unlabelled_file = _make_media_file(db_session, normal_user, "profile-put-unlabelled")
        unlabelled = _make_speaker(
            db_session, normal_user, unlabelled_file, "SPEAKER_07", profile_id=profile.id
        )
        suggested_file = _make_media_file(db_session, normal_user, "profile-put-suggested")
        _make_speaker(
            db_session,
            normal_user,
            suggested_file,
            "SPEAKER_03",
            profile_id=profile.id,
            suggested_name="Bobby (Host)",
            confidence=0.9,
        )
        # Shares a file with a member but belongs to no profile: a profile rename
        # must not sweep it along, or an unrelated speaker's chunks get rewritten.
        bystander = _make_speaker(
            db_session,
            normal_user,
            labelled_file,
            "SPEAKER_09",
            display_name="Someone Else",
        )

        resp = client.put(
            f"/api/speaker-profiles/profiles/{profile.uuid}?name=Robert",
            headers=user_token_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Robert"
        assert _queued(quiet_rename_dispatch) == {
            str(labelled_file.uuid): ["Bob"],
            str(unlabelled_file.uuid): ["SPEAKER_07"],
            str(suggested_file.uuid): ["Bobby (Host)"],
        }
        assert {c.kwargs["new_name"] for c in quiet_rename_dispatch.call_args_list} == {"Robert"}

        # The other half of the unit: Postgres must now hold the name the
        # propagation just wrote into the index, or the next reindex reverts it.
        db_session.refresh(unlabelled)
        assert unlabelled.display_name == "Robert"
        db_session.refresh(bystander)
        assert bystander.display_name == "Someone Else"

    def test_two_members_in_one_file_coalesce_into_a_single_task(
        self, client, db_session, normal_user, user_token_headers, quiet_rename_dispatch
    ):
        """Two tasks over one file would each rewrite the same ``speakers`` array.

        The loser of the version conflict is silently dropped, so the coalescing
        in ``dispatch_speaker_rename`` is correctness, not efficiency.
        """
        profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Bob")
        db_session.add(profile)
        db_session.flush()

        media_file = _make_media_file(db_session, normal_user, "profile-put-coalesce")
        _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_00",
            profile_id=profile.id,
            display_name="Bob",
        )
        _make_speaker(db_session, normal_user, media_file, "SPEAKER_07", profile_id=profile.id)

        resp = client.put(
            f"/api/speaker-profiles/profiles/{profile.uuid}?name=Robert",
            headers=user_token_headers,
        )

        assert resp.status_code == 200, resp.text
        assert quiet_rename_dispatch.call_count == 1, (
            "two diarized speakers in ONE file must coalesce into one "
            "update_by_query, not race each other"
        )
        assert _queued(quiet_rename_dispatch) == {str(media_file.uuid): ["Bob", "SPEAKER_07"]}

    def test_an_update_that_does_not_change_the_name_queues_nothing(
        self, client, db_session, normal_user, user_token_headers, quiet_rename_dispatch
    ):
        """A description edit touches no indexed field — dispatching would be noise.

        The member deliberately carries **no** ``display_name``, so it is indexed
        under ``SPEAKER_00`` while the profile is called ``Bob``. That gap is what
        makes this test able to fail: an implementation that re-applied the profile
        name on every update (rather than only on a name change) would relabel this
        speaker and queue a real ``SPEAKER_00 -> Bob`` rewrite. Had the member
        already been labelled ``Bob``, both assertions would hold either way.
        """
        profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Bob")
        db_session.add(profile)
        db_session.flush()

        media_file = _make_media_file(db_session, normal_user, "profile-put-desc-only")
        member = _make_speaker(
            db_session, normal_user, media_file, "SPEAKER_00", profile_id=profile.id
        )

        resp = client.put(
            f"/api/speaker-profiles/profiles/{profile.uuid}?description=Podcast+host",
            headers=user_token_headers,
        )

        assert resp.status_code == 200, resp.text
        # Positive control: the request really did update something, so a
        # not-called assertion cannot pass on a rejected or no-op request.
        assert resp.json()["description"] == "Podcast host"
        assert resp.json()["name"] == "Bob"
        quiet_rename_dispatch.assert_not_called()
        db_session.refresh(member)
        assert member.display_name is None, (
            "an update that changed no name must not relabel the profile's members"
        )


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

    def test_a_linked_speaker_indexed_under_a_suggestion_reports_the_suggestion_as_old_name(
        self, db_session, normal_user
    ):
        """Follow-up to issue #605: this call site still computed the ad hoc
        ``display_name or name`` chain directly instead of going through
        ``canonical_speaker_label_for_row``, the SAME resolver the chunk-index
        writers use. A linked speaker with no ``display_name`` but a confident
        ``suggested_name`` (>= the 0.75 threshold) is indexed under the
        SUGGESTION, exactly the shape of speaker 74070 from #605's own repro.
        The old chain computed the raw diarizer label here, so
        ``_propagate_speaker_rename_to_chunks``'s ``update_by_query`` would
        match nothing against the real indexed text and log ``status:
        success`` while the drift survived.
        """
        from app.api.endpoints.speakers import _handle_update_profile_action

        profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=normal_user.id, name="Host")
        db_session.add(profile)
        db_session.flush()

        media_file = _make_media_file(db_session, normal_user, "suggestion-linked")
        _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_01",
            profile_id=profile.id,
            suggested_name="Joe Rogan (Host)",
            confidence=0.9,
        )

        renames = _handle_update_profile_action(profile.id, "Joe Rogan", normal_user, db_session)

        assert renames == [(str(media_file.uuid), "Joe Rogan (Host)")], (
            "the real indexed label is the confident suggestion, not the raw "
            "diarizer name — the old ad hoc chain computed 'SPEAKER_01' here, "
            "which a propagation update_by_query would then match nothing "
            "against"
        )


class TestRetroactiveAutoApply:
    def test_auto_applied_match_reports_the_stale_chunk_name(
        self, db_session, normal_user, quiet_opensearch
    ):
        """The batch path renames other files' speakers; each one leaves stale chunks.

        Issue #605: the rename is now recorded on a ``SpeakerRenameTracker``
        (``before`` computed by the caller via ``canonical_speaker_label_for_row``,
        the SAME resolver the chunk-index writers use) rather than returned as a
        bespoke ``(file_uuid, old_name)`` tuple — the caller may need to batch
        heterogeneous after-values across many matched speakers in one pass.
        """
        from app.api.endpoints.speaker_update import _apply_high_confidence_match
        from app.services.speaker_rename_tracker import SpeakerRenameTracker
        from app.utils.speaker_labels import canonical_speaker_label_for_row

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
        before = canonical_speaker_label_for_row(matched)
        tracker = SpeakerRenameTracker()
        _apply_high_confidence_match(db_session, matched, trigger, tracker, before)

        assert tracker.pending == [(target_file.id, "SPEAKER_03", "Dana")]
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


class TestRejectSuggestionPropagation:
    """Issue #605: rejecting a confident suggestion moves the canonical label
    back to the raw diarizer name, and the chunk plane must be told — this
    writer (``_reject_speaker_suggestion``) previously dispatched nothing."""

    def test_rejecting_a_confident_suggestion_queues_the_revert_to_the_raw_label(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        media_file = _make_media_file(db_session, normal_user, "reject-suggestion")
        speaker = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_01",
            suggested_name="Joe Rogan (Host)",
            confidence=0.9,
            suggestion_source="llm_analysis",
        )

        with patch(_DELAY) as delay_mock:
            resp = client.post(
                f"/api/speakers/{speaker.uuid}/verify",
                params={"action": "reject"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["old_names"] == ["Joe Rogan (Host)"]
        assert delay_mock.call_args.kwargs["new_name"] == "SPEAKER_01"

        db_session.refresh(speaker)
        assert speaker.suggested_name is None, (
            "issue #605's second bug: confidence alone used to be cleared, leaving "
            "suggested_name behind for any reader checking it without confidence "
            "(this module's own was_auto_labeled)"
        )
        assert speaker.confidence is None
        assert speaker.suggestion_source == "llm_analysis", (
            "suggestion_source must survive rejection — it is "
            "task_detection_service's only signal that LLM speaker ID already ran "
            "on this file. Nulling it here (as an earlier version of this fix did) "
            "made a fully-rejected file look never-identified and re-offered the "
            "exact suggestion the user just rejected (audit follow-up to #603)."
        )

    def test_rejecting_a_below_threshold_suggestion_queues_nothing(
        self, client, db_session, normal_user, user_token_headers, quiet_opensearch
    ):
        """Control: the canonical label was already the raw name (a sub-threshold
        suggestion never won), so rejecting it must not dispatch a no-op rewrite."""
        media_file = _make_media_file(db_session, normal_user, "reject-weak-suggestion")
        speaker = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_01",
            suggested_name="Maybe Someone",
            confidence=0.6,
            suggestion_source="voice_match",
        )

        with patch(_DELAY) as delay_mock:
            resp = client.post(
                f"/api/speakers/{speaker.uuid}/verify",
                params={"action": "reject"},
                headers=user_token_headers,
            )

        assert resp.status_code == 200, resp.text
        delay_mock.assert_not_called()


class TestReprocessSpeakerLLMStageClear:
    """Issue #605: clearing suggestions for a reprocess must propagate. Nulling a
    confident suggestion moves the canonical label back to the raw diarizer name
    with NO ``display_name`` write at all — one of five writers that dispatched
    nothing until this fix."""

    def test_clearing_suggestions_queues_propagation_for_every_moved_speaker(
        self, db_session, normal_user, quiet_opensearch
    ):
        from app.api.endpoints.files.reprocess import clear_selective_data

        media_file = _make_media_file(db_session, normal_user, "reprocess-speaker-llm")
        moved = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_00",
            suggested_name="Priya",
            confidence=0.9,
            suggestion_source="llm_analysis",
        )
        # Control, same call: a sub-threshold suggestion's canonical label was
        # already the raw name, so clearing it must not dispatch a no-op.
        unmoved = _make_speaker(
            db_session,
            normal_user,
            media_file,
            "SPEAKER_01",
            suggested_name="Maybe Bob",
            confidence=0.6,
            suggestion_source="llm_analysis",
        )

        with patch(_DELAY) as delay_mock:
            clear_selective_data(db_session, media_file, ["speaker_llm"])

        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["file_uuid"] == str(media_file.uuid)
        assert delay_mock.call_args.kwargs["old_names"] == ["Priya"]
        assert delay_mock.call_args.kwargs["new_name"] == "SPEAKER_00"

        db_session.refresh(moved)
        db_session.refresh(unmoved)
        assert moved.suggested_name is None
        assert moved.confidence is None
        assert unmoved.suggested_name is None, "the stage clear still nulls it"


class TestSpeakerIdentificationPredictionPropagation:
    """Issue #605: an LLM prediction crossing the confidence threshold moves the
    canonical label with no ``display_name`` write — a clean ingest indexes
    chunks under the raw diarizer label, this task's prediction lands after
    (``enrich_and_dispatch`` dispatches indexing before speaker ID), and until
    this fix nothing reconciled the two."""

    def test_a_confident_prediction_queues_propagation(
        self, db_session, normal_user, quiet_opensearch
    ):
        from contextlib import contextmanager

        from app.tasks.speaker_identification_task import _store_speaker_predictions

        media_file = _make_media_file(db_session, normal_user, "llm-prediction")
        speaker = _make_speaker(db_session, normal_user, media_file, "SPEAKER_01")

        predictions = {
            "speaker_predictions": [
                {
                    "speaker_label": "SPEAKER_01",
                    "predicted_name": "Joe Rogan (Host)",
                    "confidence": 0.9,
                }
            ]
        }

        @contextmanager
        def _yield_test_session():
            yield db_session

        with (
            patch(
                "app.tasks.speaker_identification_task.session_scope",
                _yield_test_session,
            ),
            patch(_DELAY) as delay_mock,
        ):
            _store_speaker_predictions(media_file.id, predictions)

        delay_mock.assert_called_once()
        assert delay_mock.call_args.kwargs["old_names"] == ["SPEAKER_01"]
        assert delay_mock.call_args.kwargs["new_name"] == "Joe Rogan (Host)"

        # Mock bookkeeping alone would pass even if the prediction never reached
        # Postgres. Assert the durable write too.
        db_session.refresh(speaker)
        assert speaker.suggested_name == "Joe Rogan (Host)"
        assert speaker.confidence == 0.9
        assert speaker.suggestion_source == "llm_analysis"

    def test_a_weak_prediction_queues_nothing(self, db_session, normal_user, quiet_opensearch):
        """Over-firing control: a sub-threshold prediction must not dispatch — a
        detector that fires on every suggestion write regardless of whether the
        label actually moved would queue an ``update_by_query`` per speaker per
        LLM identification run, not just extra noise."""
        from contextlib import contextmanager

        from app.tasks.speaker_identification_task import _store_speaker_predictions

        media_file = _make_media_file(db_session, normal_user, "llm-weak-prediction")
        speaker = _make_speaker(db_session, normal_user, media_file, "SPEAKER_01")

        predictions = {
            "speaker_predictions": [
                {
                    "speaker_label": "SPEAKER_01",
                    "predicted_name": "Maybe Someone",
                    "confidence": 0.6,
                }
            ]
        }

        @contextmanager
        def _yield_test_session():
            yield db_session

        with (
            patch(
                "app.tasks.speaker_identification_task.session_scope",
                _yield_test_session,
            ),
            patch(_DELAY) as delay_mock,
        ):
            _store_speaker_predictions(media_file.id, predictions)

        delay_mock.assert_not_called()

        # The suggestion itself is still written below the propagation
        # threshold — only the dispatch is withheld, not the write.
        db_session.refresh(speaker)
        assert speaker.suggested_name == "Maybe Someone"
        assert speaker.confidence == 0.6


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
