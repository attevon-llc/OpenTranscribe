"""Every writer of a speaker's name or profile link records where it came from (#1280).

``Speaker.identity_source`` is what lets a privacy switch hide, and a purge delete, only what a
voiceprint made. A writer that forgets to record it leaves a voiceprint-made name looking like a
person's (never hidden, never purged) or a person's name looking like a voiceprint's (hidden and
deleted). So each writer has a case here, and every "a person did this" case also proves the
name still reads while voiceprints are off.

Real rows and the real writers; OpenSearch is answered by the recorders in ``speaker_os_seams``
and ``speaker_assignment_seams``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from app.core import constants
from app.models.media import Speaker
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.speaker_clustering_service import SpeakerClusteringService
from app.services.speaker_matching_service import SpeakerMatchingService
from app.utils.speaker_labels import canonical_speaker_label_for_row
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

_RENAME_DELAY = "app.tasks.rename_propagation_task.propagate_speaker_rename.delay"
_UPDATE_TASK = "app.tasks.speaker_update_task.process_speaker_update_background.delay"


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def _fresh(db, speaker_id: int) -> Speaker:
    db.expire_all()
    row: Speaker | None = db.get(Speaker, speaker_id)
    assert row is not None
    return row


def _switch_off_for_user(db, user) -> None:
    """The owner's personal voiceprint switch, off: the user layer of the policy."""
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def _org_off(db) -> Organization:
    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db.add(org)
    db.flush()
    db.add(OrganizationPolicy(organization_id=org.id, voiceprint_matching_enabled=False))
    db.flush()
    return org


def _shown_while_off(db, user, speaker_id: int) -> str | None:
    """The speaker's ``display_name`` as a response would carry it with voiceprints off."""
    _switch_off_for_user(db, user)
    row = _fresh(db, speaker_id)
    redact_speaker_rows(db, [row])
    return row.display_name  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# The machine writers
# --------------------------------------------------------------------------- #


def _auto_accept(db, user, speaker, match_name: str, *, profile_id: int | None = None) -> None:
    """Run the real upload-time matcher with one auto-accepted match."""
    service = SpeakerMatchingService(db, embedding_service=None)
    match = {
        "confidence": 0.93,
        "suggested_name": match_name,
        "auto_accept": True,
        "profile_id": profile_id,
    }
    with (
        patch("app.services.speaker_matching_service.add_speaker_embedding"),
        patch("app.services.speaker_matching_service.update_speaker_profile", create=True),
        patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        patch.object(SpeakerMatchingService, "_propagate_profile_assignment"),
        patch.object(SpeakerMatchingService, "match_speaker_to_known_speakers", return_value=match),
        patch(_RENAME_DELAY),
    ):
        service.process_speaker_embeddings_native(
            media_file_id=int(speaker.media_file_id),
            user_id=int(user.id),
            native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
        )


def test_an_auto_accepted_name_is_recorded_as_a_voiceprint(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()

    _auto_accept(db_session, normal_user, speaker, "Alice", profile_id=profile.id)

    row = _fresh(db_session, speaker.id)
    assert (row.display_name, row.profile_id, row.verified) == ("Alice", profile.id, True)
    assert row.identity_source == "voiceprint"


def test_an_auto_accepted_name_is_hidden_once_voiceprints_are_off(db_session, normal_user):
    """The row a person's accept leaves is byte-identical; only the source tells them apart."""
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()
    _auto_accept(db_session, normal_user, speaker, "Alice", profile_id=profile.id)

    assert _fresh(db_session, speaker.id).display_name == "Alice", "control: shown while on"
    assert _shown_while_off(db_session, normal_user, speaker.id) is None


def test_auto_accept_onto_a_typed_name_records_only_the_link(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        identity_source="human",
    )
    db_session.commit()

    _auto_accept(db_session, normal_user, speaker, "Alice", profile_id=profile.id)

    row = _fresh(db_session, speaker.id)
    assert (row.profile_id, row.identity_source) == (profile.id, "voiceprint_link")
    assert _shown_while_off(db_session, normal_user, speaker.id) == "Alice", (
        "the typed name stays; only the machine's link is hidden"
    )


def test_propagation_records_a_link_and_never_downgrades_a_voiceprint(
    db_session, normal_user, speaker_assignment_seams
):
    profile = make_profile(db_session, normal_user, "Alice")
    seen = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    named = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        identity_source="human",
    )
    db_session.commit()
    speaker_assignment_seams["voiceprints"].update(
        {str(seen.uuid): VOICE_A, str(named.uuid): VOICE_A}
    )
    service = SpeakerMatchingService(db_session, embedding_service=None)

    with patch.object(
        service, "_get_speaker_embedding_for_propagation", return_value=(VOICE_A, "x")
    ):
        service._propagate_profile_assignment(int(seen.id), int(profile.id), int(normal_user.id))

    row = _fresh(db_session, named.id)
    assert row.profile_id == profile.id, "control: the look-alike voice was linked"
    assert row.identity_source == "voiceprint_link"

    from app.utils import speaker_identity_source as ids

    machine_named = Speaker(display_name="Alice", identity_source="voiceprint")
    ids.record_machine_link(machine_named)
    assert machine_named.identity_source == "voiceprint", "a voiceprint is never downgraded"


def test_retroactive_auto_apply_is_a_voiceprint(db_session, normal_user, speaker_assignment_seams):
    from app.api.endpoints.speaker_update import trigger_retroactive_matching

    profile = make_profile(db_session, normal_user, "Alice")
    labelled = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="human",
    )
    other = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    speaker_assignment_seams["voiceprints"].update(
        {str(labelled.uuid): VOICE_A, str(other.uuid): VOICE_A}
    )
    db_session.commit()

    with (
        patch("app.api.endpoints.speaker_update._send_bulk_update_notification"),
        patch(_RENAME_DELAY),
    ):
        result = trigger_retroactive_matching(labelled, db_session)

    assert result["auto_applied_count"] == 1, "control: the other recording was auto-labelled"
    row = _fresh(db_session, other.id)
    assert (row.display_name, row.verified) == ("Alice", True)
    assert row.identity_source == "voiceprint"
    assert _fresh(db_session, labelled.id).identity_source == "human", "the trigger is untouched"


def test_an_upload_cluster_join_is_a_voiceprint_and_a_merge_join_is_human(
    db_session, normal_user, speaker_assignment_seams
):
    profile = make_profile(db_session, normal_user, "Alice")
    member = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    joiner = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    merged = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    for speaker in (member, joiner, merged):
        speaker_assignment_seams["voiceprints"][str(speaker.uuid)] = VOICE_A
    promoted = make_cluster(db_session, normal_user, member, promoted_to=profile.id, label="Alice")
    source = make_cluster(db_session, normal_user, merged, label="loose")
    speaker_assignment_seams["clusters"][str(promoted.uuid)] = VOICE_A
    service = SpeakerClusteringService(db_session)

    with patch(_RENAME_DELAY):
        service._join(joiner, promoted, 0.9, None)
        db_session.commit()
        assert service.merge_clusters(str(source.uuid), str(promoted.uuid), normal_user.id)

    assert _fresh(db_session, joiner.id).identity_source == "voiceprint"
    merged_row = _fresh(db_session, merged.id)
    assert (merged_row.display_name, merged_row.identity_source) == ("Alice", "human")


# --------------------------------------------------------------------------- #
# A person's action makes it theirs
# --------------------------------------------------------------------------- #


def _machine_named(db, user, *, name: str = "Alice", linked: bool = True):
    """A speaker a voiceprint named, ready for a person to act on."""
    profile = make_profile(db, user, name)
    speaker = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name=name,
        verified=True,
        profile_id=profile.id if linked else None,
        identity_source="voiceprint",
        suggested_name=name,
        suggestion_source="profile_match",
        confidence=0.9,
    )
    db.commit()
    return speaker, profile


def _put_display_name(client, headers, speaker, profile, user, db):
    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        return client.put(
            f"/api/speakers/{speaker.uuid}", json={"display_name": "Zed"}, headers=headers
        )


def _put_profile_action(client, headers, speaker, profile, user, db):
    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        return client.put(
            f"/api/speakers/{speaker.uuid}",
            json={"display_name": "Zed", "profile_action": "create_new_profile"},
            headers=headers,
        )


def _put_verified(client, headers, speaker, profile, user, db):
    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        return client.put(f"/api/speakers/{speaker.uuid}", json={"verified": True}, headers=headers)


def _verify_accept(client, headers, speaker, profile, user, db):
    other = make_profile(db, user, "Zed")
    db.commit()
    with patch(_RENAME_DELAY):
        return client.post(
            f"/api/speakers/{speaker.uuid}/verify",
            params={"action": "accept", "profile_uuid": str(other.uuid)},
            headers=headers,
        )


def _verify_create_profile(client, headers, speaker, profile, user, db):
    with patch(_RENAME_DELAY):
        return client.post(
            f"/api/speakers/{speaker.uuid}/verify",
            params={"action": "create_profile", "profile_name": "Zed"},
            headers=headers,
        )


def _assign_profile(client, headers, speaker, profile, user, db):
    other = make_profile(db, user, "Zed")
    db.commit()
    with (
        patch(_RENAME_DELAY),
        patch("app.tasks.speaker_update_task.process_speaker_update_background.delay"),
    ):
        return client.post(
            f"/api/speaker-profiles/speakers/{speaker.uuid}/assign-profile",
            params={"profile_uuid": str(other.uuid)},
            headers=headers,
        )


def _batch(action: str, **extra: Any):
    def call(client, headers, speaker, profile, user, db):
        body: dict[str, Any] = {"speaker_uuids": [str(speaker.uuid)], "action": action, **extra}
        if action == "assign":
            other = make_profile(db, user, "Zed")
            db.commit()
            body["profile_uuid"] = str(other.uuid)
        with patch(_RENAME_DELAY):
            return client.post("/api/speaker-clusters/batch-verify", json=body, headers=headers)

    return call


_HUMAN_ACTIONS = {
    "put_display_name": _put_display_name,
    "put_profile_action": _put_profile_action,
    "put_verified": _put_verified,
    "verify_accept": _verify_accept,
    "verify_create_profile": _verify_create_profile,
    "assign_profile": _assign_profile,
    "batch_accept": _batch("accept"),
    "batch_name": _batch("name", display_name="Zed"),
    "batch_assign": _batch("assign"),
}


@pytest.mark.parametrize("action", sorted(_HUMAN_ACTIONS))
def test_each_human_action_turns_a_voiceprint_identity_into_a_human_one(
    action, client, user_token_headers, db_session, normal_user
):
    speaker, profile = _machine_named(db_session, normal_user)
    headers = _headers(user_token_headers)

    response = _HUMAN_ACTIONS[action](client, headers, speaker, profile, normal_user, db_session)

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    assert row.identity_source == "human", f"{action} left the source as {row.identity_source!r}"
    assert row.display_name, "control: the action named the speaker"
    assert _shown_while_off(db_session, normal_user, speaker.id) == row.display_name


def test_promoting_a_cluster_makes_its_members_human(db_session, normal_user):
    speaker, _profile = _machine_named(db_session, normal_user, linked=False)
    cluster = make_cluster(db_session, normal_user, speaker)

    with (
        patch(_RENAME_DELAY),
        patch(
            "app.services.profile_embedding_service.ProfileEmbeddingService.update_profile_embedding"
        ),
    ):
        profile = SpeakerClusteringService(db_session).promote_cluster_to_profile(
            str(cluster.uuid), "Zed", int(normal_user.id)
        )

    assert profile is not None
    row = _fresh(db_session, speaker.id)
    assert (row.display_name, row.identity_source) == ("Zed", "human")
    assert _shown_while_off(db_session, normal_user, speaker.id) == "Zed"


def test_creating_a_new_profile_from_a_typed_name_is_a_human_identity(db_session, normal_user):
    from app.api.endpoints.speakers import _handle_create_new_profile_action

    speaker, _ = _machine_named(db_session, normal_user, linked=False)

    _handle_create_new_profile_action(speaker, "Zed", normal_user, db_session)
    db_session.commit()

    row = _fresh(db_session, speaker.id)
    assert row.profile_id is not None, "control: the profile was created and linked"
    assert row.identity_source == "human"


def test_creating_a_named_speaker_is_a_human_identity_and_an_unnamed_one_is_not(
    client, user_token_headers, db_session, normal_user
):
    media_file = make_file(db_session, normal_user)
    db_session.commit()
    headers = _headers(user_token_headers)

    named = client.post(
        "/api/speakers",
        params={"media_file_uuid": str(media_file.uuid)},
        json={"name": "SPEAKER_07", "display_name": "Zed"},
        headers=headers,
    )
    unnamed = client.post(
        "/api/speakers",
        params={"media_file_uuid": str(media_file.uuid)},
        json={"name": "SPEAKER_08"},
        headers=headers,
    )

    assert named.status_code == unnamed.status_code == 200, (named.text, unnamed.text)
    sources = {
        r.name: r.identity_source
        for r in db_session.query(Speaker).filter(Speaker.media_file_id == media_file.id)
    }
    assert sources == {"SPEAKER_07": "human", "SPEAKER_08": None}


@pytest.mark.parametrize("profile_exists", [False, True])
def test_a_name_typed_for_a_speaker_with_no_profile_yet_is_human(
    profile_exists, db_session, normal_user
):
    """``auto_create_or_assign_profile`` links the typed name to a profile (an existing
    same-named one, or a new one); either way the link is the person's."""
    from app.api.endpoints.speaker_update import auto_create_or_assign_profile

    speaker, _ = _machine_named(db_session, normal_user, linked=False)
    if profile_exists:
        make_profile(db_session, normal_user, "Zed")
    speaker.display_name = "Zed"
    with patch("app.api.endpoints.speaker_update._update_profile_embedding"):
        assert auto_create_or_assign_profile(speaker, "Zed", db_session)
    db_session.commit()

    row = _fresh(db_session, speaker.id)
    assert (row.display_name, row.identity_source) == ("Zed", "human")
    assert row.profile_id is not None


# --------------------------------------------------------------------------- #
# Clearing, unlinking
# --------------------------------------------------------------------------- #


def _clear_name(client, headers, speaker):
    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        return client.put(
            f"/api/speakers/{speaker.uuid}", json={"display_name": ""}, headers=headers
        )


@pytest.mark.parametrize(
    ("before", "linked", "expected"),
    [
        (
            "voiceprint",
            True,
            "voiceprint_link",
        ),  # the machine's link remains, hideable and purgeable
        ("human", True, "human"),
        ("voiceprint", False, None),  # nothing left to describe
        ("human", False, None),
    ],
)
def test_clearing_a_name_settles_what_is_left(
    before, linked, expected, client, user_token_headers, db_session, normal_user
):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        verified=True,
        profile_id=profile.id if linked else None,
        identity_source=before,
    )
    db_session.commit()

    response = _clear_name(client, _headers(user_token_headers), speaker)

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    assert not row.display_name
    assert row.identity_source == expected


def test_unlinking_a_machine_link_restores_what_the_name_was(db_session, normal_user):
    """``unlink_from_profile`` (profile delete, reject, unassign) applies ``after_unlink``."""
    from app.services.speaker_cleanup import unlink_from_profile
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

    profile = make_profile(db_session, normal_user, "Alice")

    def row(display_name, source):
        return make_speaker(
            db_session,
            normal_user,
            make_file(db_session, normal_user),
            display_name=display_name,
            profile_id=profile.id,
            verified=True,
            identity_source=source,
        )

    machine_named = row("Alice", "voiceprint")  # name IS the profile's: goes with the link
    link_only = row("Bob", "voiceprint_link")  # a typed name the machine linked: stays, as NULL
    typed = row("Bob", "human")
    db_session.commit()

    unlink_from_profile(
        db_session, [machine_named, link_only, typed], int(profile.id), SpeakerRenameTracker()
    )
    db_session.commit()

    assert _fresh(db_session, machine_named.id).identity_source is None
    assert _fresh(db_session, machine_named.id).display_name is None
    kept = _fresh(db_session, link_only.id)
    assert (kept.display_name, kept.identity_source, kept.profile_id) == ("Bob", None, None)
    assert _fresh(db_session, typed.id).identity_source == "human"


def test_unassigning_from_a_promoted_cluster_unlinks_with_the_same_rule(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    machine = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="voiceprint",
    )
    keeper = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    cluster = make_cluster(db_session, normal_user, machine, keeper, promoted_to=profile.id)

    with patch(_RENAME_DELAY):
        SpeakerClusteringService(db_session).unassign_speakers(
            str(cluster.uuid), [str(machine.uuid)], normal_user.id, blacklist=True
        )

    row = _fresh(db_session, machine.id)
    assert (row.profile_id, row.display_name, row.identity_source) == (None, None, None)


# --------------------------------------------------------------------------- #
# Merging two speakers: provenance follows the name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source_identity", "target_identity"),
    [("voiceprint", "human"), ("human", "voiceprint")],
)
def test_merging_two_speakers_leaves_the_survivors_identity_as_it_was(
    source_identity, target_identity, client, user_token_headers, db_session, normal_user
):
    """The survivor keeps its OWN name and link, so it keeps its own provenance too.

    A person merging two diarized speakers does not confirm a machine name: a ``voiceprint``
    survivor stays one, and a ``human`` survivor is not downgraded by a machine-named source.
    """
    profile_a = make_profile(db_session, normal_user, "Alice")
    profile_b = make_profile(db_session, normal_user, "Bea")
    media_file = make_file(db_session, normal_user)
    source = make_speaker(
        db_session,
        normal_user,
        media_file,
        "SPEAKER_00",
        display_name="Alice",
        profile_id=profile_a.id,
        identity_source=source_identity,
    )
    target = make_speaker(
        db_session,
        normal_user,
        media_file,
        "SPEAKER_01",
        display_name="Bea",
        profile_id=profile_b.id,
        identity_source=target_identity,
    )
    db_session.commit()

    with (
        patch("app.tasks.speaker_merge_task.process_speaker_merge_background.delay"),
        patch("app.services.search.reindex_dispatch.dispatch_transcript_reindex"),
    ):
        response = client.post(
            f"/api/speakers/{source.uuid}/merge/{target.uuid}", headers=_headers(user_token_headers)
        )

    assert response.status_code == 200, response.text
    survivor = _fresh(db_session, target.id)
    assert (survivor.display_name, survivor.profile_id) == ("Bea", profile_b.id)
    assert survivor.identity_source == target_identity
    assert db_session.get(Speaker, source.id) is None, "control: the source really was merged away"


def test_the_label_a_tracker_records_follows_the_source_set_before_it(db_session, normal_user):
    """The 'after' label is computed with ``identity_source`` already set (order matters)."""
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    org = _org_off(db_session)
    speaker.media_file.organization_id = org.id
    db_session.commit()
    speaker.display_name = "Alice"
    speaker.verified = True

    from app.utils.speaker_identity_source import record_machine_identity

    record_machine_identity(speaker, previous_display_name=None)

    assert canonical_speaker_label_for_row(speaker) == speaker.name, (
        "off: a voiceprint-made name is not the label"
    )
