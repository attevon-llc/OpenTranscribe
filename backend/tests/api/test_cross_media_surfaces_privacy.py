"""Cross-recording speaker surfaces judge every occurrence by its OWN file's voiceprint switch.

The cross-media list, its two admin debug twins and the speaker media preview used to match
and print the raw ``profile_id`` / ``display_name`` columns. With voiceprints off, a person's
named speaker then listed files that only a voiceprint link connected (with the match
confidence), and a voiceprint-named speaker showed the profile it had been linked to. Each hiding
case has an "on" control and a person-made control so a blanket "hide everything" cannot pass.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.api.endpoints import speakers as speakers_module
from app.core import constants
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.tasks import speaker_merge_task
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _headers(h: dict) -> dict:
    return {k: v for k, v in h.items() if not k.startswith("_")}


def _off(db, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def _pair(db, user, *, machine_source="voiceprint"):
    """A person-named speaker and a machine-linked one on the same profile, in two files."""
    profile = make_profile(db, user, "Alice")
    f_human, f_machine = make_file(db, user), make_file(db, user)
    human = make_speaker(
        db,
        user,
        f_human,
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="human",
    )
    machine = make_speaker(
        db,
        user,
        f_machine,
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source=machine_source,
        confidence=0.91,
    )
    db.commit()
    return human, machine, f_human, f_machine


def _cross_media(client, headers, speaker) -> list[dict]:
    r = client.get(f"/api/speakers/{speaker.uuid}/cross-media", headers=_headers(headers))
    assert r.status_code == 200, r.text
    rows: list[dict] = r.json()
    return rows


def test_cross_media_lists_the_machine_linked_file_while_on(
    client, user_token_headers, db_session, normal_user
):
    human, _, _, f_machine = _pair(db_session, normal_user)

    shown = {row["media_file_id"] for row in _cross_media(client, user_token_headers, human)}

    assert str(f_machine.uuid) in shown


def test_cross_media_omits_a_file_only_a_voiceprint_link_connects_while_off(
    client, user_token_headers, db_session, normal_user
):
    human, _, f_human, f_machine = _pair(db_session, normal_user)
    _off(db_session, normal_user)

    shown = {row["media_file_id"] for row in _cross_media(client, user_token_headers, human)}

    assert shown == {str(f_human.uuid)}, f"the machine-linked file is not disclosed: {shown}"


def test_cross_media_keeps_a_person_made_link_while_off(
    client, user_token_headers, db_session, normal_user
):
    human, _, f_human, _ = _pair(db_session, normal_user)
    profile_id = human.profile_id
    f_other = make_file(db_session, normal_user)
    make_speaker(
        db_session,
        normal_user,
        f_other,
        display_name="Alice",
        profile_id=profile_id,
        verified=True,
        identity_source="human",
    )
    db_session.commit()
    _off(db_session, normal_user)

    shown = {row["media_file_id"] for row in _cross_media(client, user_token_headers, human)}

    assert shown == {str(f_human.uuid), str(f_other.uuid)}


def test_cross_media_of_a_voiceprint_speaker_shows_only_itself_without_its_match_while_off(
    client, user_token_headers, db_session, normal_user
):
    _, machine, _, f_machine = _pair(db_session, normal_user)
    _off(db_session, normal_user)

    rows = _cross_media(client, user_token_headers, machine)

    assert [row["media_file_id"] for row in rows] == [str(f_machine.uuid)]
    assert rows[0]["confidence"] is None, "the voiceprint match confidence is withheld"
    assert rows[0]["verified"] is False


def test_cross_media_by_name_omits_voiceprint_named_speakers_while_off(
    client, user_token_headers, db_session, normal_user
):
    """The name branch (no profile): a voiceprint-made namesake is not offered while off."""
    f_human, f_machine = make_file(db_session, normal_user), make_file(db_session, normal_user)
    human = make_speaker(
        db_session, normal_user, f_human, display_name="Alice", identity_source="human"
    )
    profile = make_profile(db_session, normal_user, "Alice")
    make_speaker(
        db_session,
        normal_user,
        f_machine,
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="voiceprint",
        confidence=0.9,
    )
    db_session.commit()

    on = {row["media_file_id"] for row in _cross_media(client, user_token_headers, human)}
    _off(db_session, normal_user)
    off = {row["media_file_id"] for row in _cross_media(client, user_token_headers, human)}

    assert on == {str(f_human.uuid), str(f_machine.uuid)}, "control: listed while on"
    assert off == {str(f_human.uuid)}


def test_each_occurrence_is_judged_by_its_own_files_scope(db_session, normal_user):
    """One owner, two scopes: the organization's switch is off, the personal one is on."""
    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    db_session.add(OrganizationPolicy(organization_id=org.id, voiceprint_matching_enabled=False))
    profile = make_profile(db_session, normal_user, "Alice")
    files = {name: make_file(db_session, normal_user) for name in ("self", "on", "off", "off_h")}
    files["off"].organization_id = org.id
    files["off_h"].organization_id = org.id
    kw = {"display_name": "Alice", "profile_id": profile.id, "verified": True}
    subject = make_speaker(db_session, normal_user, files["self"], identity_source="human", **kw)
    make_speaker(db_session, normal_user, files["on"], identity_source="voiceprint", **kw)
    make_speaker(db_session, normal_user, files["off"], identity_source="voiceprint", **kw)
    make_speaker(db_session, normal_user, files["off_h"], identity_source="human", **kw)
    db_session.commit()

    rows = speakers_module._get_profile_based_occurrences(subject, normal_user, db_session)

    shown = {row["media_file_id"] for row in rows}
    assert shown == {str(files[k].uuid) for k in ("self", "on", "off_h")}


def test_the_media_preview_withholds_a_voiceprint_made_name_while_off(
    client, user_token_headers, db_session, normal_user
):
    f = make_file(db_session, normal_user)
    sp = make_speaker(
        db_session,
        normal_user,
        f,
        display_name="Alice",
        verified=True,
        identity_source="voiceprint",
    )
    db_session.commit()
    on = client.get(
        f"/api/speaker-clusters/speakers/{sp.uuid}/media-preview",
        headers=_headers(user_token_headers),
    )
    _off(db_session, normal_user)
    off = client.get(
        f"/api/speaker-clusters/speakers/{sp.uuid}/media-preview",
        headers=_headers(user_token_headers),
    )

    assert on.status_code == off.status_code == 200, off.text
    assert on.json()["speaker_name"] == "Alice", "control: the name shows while on"
    assert off.json()["speaker_name"] == "SPEAKER_00"


def test_the_media_preview_keeps_a_persons_name_while_off(
    client, user_token_headers, db_session, normal_user
):
    f = make_file(db_session, normal_user)
    sp = make_speaker(
        db_session, normal_user, f, display_name="Alice", verified=True, identity_source="human"
    )
    db_session.commit()
    _off(db_session, normal_user)

    r = client.get(
        f"/api/speaker-clusters/speakers/{sp.uuid}/media-preview",
        headers=_headers(user_token_headers),
    )

    assert r.json()["speaker_name"] == "Alice"


def test_the_admin_name_debug_route_does_not_match_a_voiceprint_made_name_while_off(
    client, admin_token_headers, db_session, admin_user
):
    human, machine, _, f_machine = _pair(db_session, admin_user)
    _off(db_session, admin_user)

    r = client.get(
        "/api/speakers/debug/cross-media-by-name",
        params={"speaker_name": "Alice"},
        headers=_headers(admin_token_headers),
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["speakers_found"] == 1, "only the person-made Alice matches"
    only = body["cross_media_results"][0]
    assert only["speaker_id"] == human.id
    assert {o["speaker_id"] for o in only["occurrences"]} == {human.id}
    assert machine.id not in {o["speaker_id"] for o in only["occurrences"]}


def test_the_admin_name_debug_route_lists_both_while_on(
    client, admin_token_headers, db_session, admin_user
):
    human, machine, _, _ = _pair(db_session, admin_user)

    r = client.get(
        "/api/speakers/debug/cross-media-by-name",
        params={"speaker_name": "Alice"},
        headers=_headers(admin_token_headers),
    )

    body = r.json()
    assert body["speakers_found"] == 2
    ids = {o["speaker_id"] for res in body["cross_media_results"] for o in res["occurrences"]}
    assert ids == {human.id, machine.id}


def test_the_admin_data_debug_route_withholds_a_voiceprint_name_link_and_confidence_while_off(
    client, admin_token_headers, db_session, admin_user, monkeypatch
):
    human, machine, _, _ = _pair(db_session, admin_user)
    monkeypatch.setattr(speakers_module, "_collect_index_debug_documents", lambda uid: {})
    _off(db_session, admin_user)

    r = client.get("/api/speakers/debug/cross-media-data", headers=_headers(admin_token_headers))

    assert r.status_code == 200, r.text
    by_id = {s["id"]: s for s in r.json()["speakers"]}
    assert by_id[machine.id]["display_name"] is None
    assert by_id[machine.id]["profile_id"] is None
    assert by_id[machine.id]["confidence"] is None
    assert by_id[machine.id]["verified"] is False
    assert by_id[human.id]["display_name"] == "Alice", "a person's name stays"
    assert by_id[human.id]["profile_id"] == human.profile_id


def test_the_admin_data_debug_route_blanks_the_index_names_of_withheld_speakers(
    client, admin_token_headers, db_session, admin_user, monkeypatch
):
    human, machine, _, _ = _pair(db_session, admin_user)
    docs = {
        "opensearch_speakers": [
            {"speaker_id": machine.id, "display_name": "Alice", "profile_id": machine.profile_id},
            {"speaker_id": human.id, "display_name": "Alice", "profile_id": human.profile_id},
        ],
        "opensearch_profiles": [],
    }
    monkeypatch.setattr(speakers_module, "_collect_index_debug_documents", lambda uid: docs)
    _off(db_session, admin_user)

    r = client.get("/api/speakers/debug/cross-media-data", headers=_headers(admin_token_headers))

    shown = {d["speaker_id"]: d for d in r.json()["opensearch_speakers"]}
    assert shown[machine.id]["display_name"] is None and shown[machine.id]["profile_id"] is None
    assert shown[human.id]["display_name"] == "Alice"


def test_the_admin_data_debug_route_keeps_the_name_a_person_gave_a_machine_linked_speaker(
    client, admin_token_headers, db_session, admin_user, monkeypatch
):
    _, linked, _, _ = _pair(db_session, admin_user, machine_source="voiceprint_link")
    docs = {
        "opensearch_speakers": [
            {"speaker_id": linked.id, "display_name": "Alice", "profile_id": linked.profile_id}
        ],
        "opensearch_profiles": [],
    }
    monkeypatch.setattr(speakers_module, "_collect_index_debug_documents", lambda uid: docs)
    _off(db_session, admin_user)

    r = client.get("/api/speakers/debug/cross-media-data", headers=_headers(admin_token_headers))

    (doc,) = r.json()["opensearch_speakers"]
    assert doc["display_name"] == "Alice", "the person's name stays; only the machine link goes"
    assert doc["profile_id"] is None


@pytest.fixture
def merge_events(monkeypatch) -> list[dict]:
    """Stand in for everything the merge task does after reading its plan; returns the events."""
    import contextlib

    sent: list[dict] = []
    for name in ("_clear_speaker_video_cache", "_update_opensearch_speaker_merge"):
        monkeypatch.setattr(speakers_module, name, lambda *a, **k: None)
    monkeypatch.setattr(speakers_module, "_merge_speaker_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(speakers_module, "_reconcile_speakers_after_merge", lambda *a, **k: [])
    monkeypatch.setattr(speakers_module, "_refresh_analytics_after_merge", lambda *a, **k: None)
    monkeypatch.setattr(
        speaker_merge_task, "session_scope", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", lambda *a, **k: True)

    def record(user_id, kind, data, **kwargs) -> bool:
        sent.append(data)
        return True

    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event_for_file", record)
    return sent


@pytest.mark.parametrize(
    ("source", "off", "expected"),
    [
        ("voiceprint", True, None),
        ("voiceprint", False, "Alice"),
        ("human", True, "Alice"),
        ("voiceprint_link", True, "Alice"),
    ],
)
def test_the_merge_plan_names_only_the_visible_name(
    db_session, normal_user, monkeypatch, source, off, expected
):
    import contextlib

    survivor = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        verified=True,
        identity_source=source,
    )
    db_session.commit()
    if off:
        _off(db_session, normal_user)
    monkeypatch.setattr(
        speaker_merge_task, "session_scope", lambda: contextlib.nullcontext(db_session)
    )

    plan = speaker_merge_task._load_merge_plan(str(survivor.uuid))

    assert plan is not None
    assert plan["shown_display_name"] == expected
    assert plan["display_name"] == "Alice", "the stored value is unchanged"


def test_the_merge_notification_carries_the_plans_visible_name_not_the_stored_one(
    merge_events, monkeypatch
):
    plan = {
        "voiceprints_allowed": False,
        "id": 1,
        "uuid": "dst-uuid",
        "display_name": "Alice",
        "shown_display_name": None,
        "media_file_uuid": "11111111-1111-1111-1111-111111111111",
    }
    monkeypatch.setattr(speaker_merge_task, "_load_merge_plan", lambda uuid: plan)

    speaker_merge_task.process_speaker_merge_background.run(
        "src-uuid", "dst-uuid", 1, 10, None, None, []
    )

    assert [event["display_name"] for event in merge_events] == [""]
