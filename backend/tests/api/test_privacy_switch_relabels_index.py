"""Changing a voiceprint switch re-indexes the files whose speaker label it decides (#1280, D5).

(The durable record, the audit events and the maintenance safety net are in
``tests/unit/test_privacy_label_resync.py``; this suite is the routes-to-queue behaviour.)

The search chunks, digests and facts roster carry the canonical speaker label. A voice-matched
name is left out of that label while voiceprints are off and returns when they are on, so the
documents already in the index go stale the moment a switch moves, in BOTH directions. The write
side of every layer (the person's own switch, an organization's, the instance's) must therefore
queue the existing debounced re-index for the files it can relabel, and only those.

Re-index dispatch is replaced by a recorder: what is asserted is WHICH files are handed to the
existing mechanism, never a call sequence. Each case has its negative control (a switch that did
not change, a file no voiceprint names, another owner's or organization's file).
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.schemas.privacy import OrgPolicyUpdate
from app.services.privacy import policy_admin
from app.services.search.reindex_dispatch import ReindexDispatch
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker


@pytest.fixture
def queued(monkeypatch, db_session) -> dict[str, list]:
    """Record the files handed to the debounced re-index and to the facts regeneration.

    The ``privacy.label_resync`` task is run in place of the broker: a switch change records
    the pending resync and dispatches the task, and the task body is what queues the files.
    """
    from app.services.privacy.label_reindex import run_label_resync

    seen: dict[str, list] = {"reindex": [], "facts": []}
    monkeypatch.setattr(
        "app.tasks.privacy_label_resync_task.privacy_label_resync_task.delay",
        lambda: run_label_resync(db_session),
    )

    def reindex(*, file_id, file_uuid, user_id, priority=None):
        seen["reindex"].append(int(file_id))
        return ReindexDispatch.QUEUED

    monkeypatch.setattr("app.services.search.reindex_dispatch.queue_transcript_reindex", reindex)
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id: seen["facts"].append(int(file_id)),
    )
    return seen


def _voice_named_file(db, owner, org=None, *, verified: bool = False, source: str = "voice_match"):
    media_file = make_file(db, owner)
    media_file.organization_id = org.id if org is not None else None
    make_speaker(
        db,
        owner,
        media_file,
        suggested_name="Zylofenix Quorra",
        suggestion_source=source,
        confidence=0.91,
        verified=verified,
    )
    db.commit()
    return media_file


def test_a_users_own_switch_queues_their_voice_named_files_both_ways(
    db_session, normal_user, other_user, queued
):
    mine = _voice_named_file(db_session, normal_user)
    verified = _voice_named_file(db_session, normal_user, verified=True)
    by_llm = _voice_named_file(db_session, normal_user, source="llm_analysis")
    someone_elses = _voice_named_file(db_session, other_user)

    policy_admin.write_user_switch(db_session, normal_user.id, False)

    assert queued["reindex"] == [mine.id, verified.id]
    assert queued["facts"] == [mine.id, verified.id]
    assert by_llm.id not in queued["reindex"]
    assert someone_elses.id not in queued["reindex"]

    queued["reindex"].clear()
    policy_admin.write_user_switch(db_session, normal_user.id, True)
    assert queued["reindex"] == [mine.id, verified.id]


def test_an_organization_switch_queues_only_that_organizations_files(
    db_session, normal_user, queued
):
    org, other_org = make_org(db_session), make_org(db_session)
    inside = _voice_named_file(db_session, normal_user, org)
    elsewhere = _voice_named_file(db_session, normal_user, other_org)
    personal = _voice_named_file(db_session, normal_user)

    changed = policy_admin.apply_org_policy_update(
        db_session, org.id, normal_user.id, OrgPolicyUpdate(voiceprint_matching_enabled=False)
    )

    assert changed == ["voiceprint_matching_enabled"]
    assert queued["reindex"] == [inside.id]
    assert elsewhere.id not in queued["reindex"] and personal.id not in queued["reindex"]


def test_an_org_change_that_leaves_voiceprints_alone_queues_nothing(
    db_session, normal_user, queued
):
    org = make_org(db_session)
    _voice_named_file(db_session, normal_user, org)

    policy_admin.apply_org_policy_update(
        db_session, org.id, normal_user.id, OrgPolicyUpdate(gender_inference_enabled=False)
    )

    assert queued["reindex"] == []


def test_the_instance_switch_queues_every_voice_named_file(
    client, super_admin_token_headers, db_session, normal_user, other_user, queued
):
    first = _voice_named_file(db_session, normal_user)
    second = _voice_named_file(db_session, other_user)

    resp = client.put(
        "/api/admin/privacy-policy",
        headers=super_admin_token_headers,
        json={"voiceprint_matching_enabled": False},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert sorted(queued["reindex"]) == sorted([first.id, second.id])


def test_an_instance_write_that_changes_nothing_queues_nothing(
    client, super_admin_token_headers, db_session, normal_user, queued
):
    _voice_named_file(db_session, normal_user)

    resp = client.put(
        "/api/admin/privacy-policy",
        headers=super_admin_token_headers,
        json={"voiceprint_matching_enabled": True},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert queued["reindex"] == []


def test_a_gender_only_instance_change_queues_nothing(
    client, super_admin_token_headers, db_session, normal_user, queued
):
    _voice_named_file(db_session, normal_user)

    resp = client.put(
        "/api/admin/privacy-policy",
        headers=super_admin_token_headers,
        json={"gender_inference_enabled": False},
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert queued["reindex"] == []


def test_a_user_write_that_repeats_the_current_value_queues_nothing(
    db_session, normal_user, queued
):
    _voice_named_file(db_session, normal_user)

    policy_admin.write_user_switch(db_session, normal_user.id, True)

    assert queued["reindex"] == []


def test_a_broker_failure_never_fails_the_switch_change(
    db_session, normal_user, monkeypatch, queued
):
    def refuse(**_kw):
        return ReindexDispatch.FAILED

    monkeypatch.setattr("app.services.search.reindex_dispatch.queue_transcript_reindex", refuse)
    _voice_named_file(db_session, normal_user)

    policy_admin.write_user_switch(db_session, normal_user.id, False)

    assert policy_admin.read_user_switch(db_session, normal_user.id) is False
    assert queued["facts"] == []
