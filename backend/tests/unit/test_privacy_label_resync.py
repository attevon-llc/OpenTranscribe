"""A voiceprint switch change relabels the index durably, and says what it did (#1280).

A switch that hides or restores a voice-made name must rebuild the files whose indexed label it
decides. This suite holds the properties that make that dependable rather than hopeful:

* exactly the affected files of the changed scope are queued, and only when the value changed;
* the work is recorded BEFORE it is dispatched, survives a failed dispatch or a failed file,
  and is finished by the maintenance arm;
* a scope is only retired when every file was queued, and a newer request is never retired by
  an older pass;
* every request and every finished scope is an audit event whose counts equal what was queued
  (asserted on the JSON the audit logger wrote, not on a mock call);
* indexed labels that disagree with the policy (a ceiling resolver or consent hook that changed
  its answer, which no switch hook sees) are found and queued;
* the task is registered, included and routed (a missing include is silent).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CeleryQueues
from app.models.system_settings import SystemSettings
from app.schemas.privacy import OrgPolicyUpdate
from app.services.privacy import label_reindex
from app.services.privacy import policy
from app.services.privacy import policy_admin
from app.services.privacy.label_reindex import ResyncScope
from app.services.privacy.label_reindex import detect_label_drift
from app.services.privacy.label_reindex import read_pending
from app.services.privacy.label_reindex import request_label_resync
from app.services.privacy.label_reindex import run_label_resync
from app.services.search.reindex_dispatch import ReindexDispatch
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import build_world
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def seams(monkeypatch) -> dict[str, Any]:
    """The task dispatch, the per-file re-index and the facts dispatch, all recorded."""
    rec: dict[str, Any] = {
        "tasks": 0,
        "reindex": [],
        "facts": [],
        "outcome": ReindexDispatch.QUEUED,
    }

    def task_delay() -> None:
        rec["tasks"] += 1

    def reindex(*, file_id, file_uuid, user_id, priority=None):
        rec["reindex"].append(int(file_id))
        outcome = rec["outcome"]
        return outcome(file_id) if callable(outcome) else outcome

    monkeypatch.setattr(
        "app.tasks.privacy_label_resync_task.privacy_label_resync_task.delay", task_delay
    )
    monkeypatch.setattr("app.services.search.reindex_dispatch.queue_transcript_reindex", reindex)
    monkeypatch.setattr(
        "app.tasks.ingest_artifacts_task.dispatch_file_facts",
        lambda file_id: rec["facts"].append(int(file_id)),
    )
    return rec


@pytest.fixture
def audit(monkeypatch) -> Iterator[list[dict[str, Any]]]:
    """The JSON events the audit logger wrote while the test ran."""
    from app.auth.audit import audit_logger

    events: list[dict[str, Any]] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                events.append(json.loads(record.getMessage()))
            except ValueError:
                pass

    monkeypatch.setattr(settings, "AUDIT_LOG_FORMAT", "json")
    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", False)
    handler = Capture()
    audit_logger._logger.addHandler(handler)
    yield events
    audit_logger._logger.removeHandler(handler)


def _resync_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["event_type"] == "privacy.labels.resync"]


def _suggested_file(db, owner, org=None):
    """A file named only by a voiceprint suggestion (the pre-provenance shape)."""
    media_file = make_file(db, owner)
    media_file.organization_id = org.id if org is not None else None
    make_speaker(
        db,
        owner,
        media_file,
        suggested_name="Ophelia Marchbanks",
        suggestion_source="voice_match",
        confidence=0.9,
    )
    db.commit()
    return media_file


def _human_file(db, owner, org=None):
    media_file = make_file(db, owner)
    media_file.organization_id = org.id if org is not None else None
    make_speaker(db, owner, media_file, display_name="Bob Brackett", identity_source="human")
    db.commit()
    return media_file


def _org_off(db, org, owner):
    return policy_admin.apply_org_policy_update(
        db, org.id, owner.id, OrgPolicyUpdate(voiceprint_matching_enabled=False)
    )


def test_turning_an_org_off_queues_exactly_its_affected_files(db_session, normal_user, seams):
    org, other = make_org(db_session), make_org(db_session)
    voiceprint = build_world(db_session, normal_user, organization=org).file
    suggested = _suggested_file(db_session, normal_user, org)
    human_only = _human_file(db_session, normal_user, org)
    elsewhere = build_world(db_session, normal_user, organization=other).file
    personal = build_world(db_session, normal_user).file
    db_session.commit()

    _org_off(db_session, org, normal_user)
    outcomes = run_label_resync(db_session)

    assert sorted(seams["reindex"]) == sorted([voiceprint.id, suggested.id])
    assert human_only.id not in seams["reindex"]
    assert elsewhere.id not in seams["reindex"] and personal.id not in seams["reindex"]
    assert sorted(seams["facts"]) == sorted(seams["reindex"])
    assert [o.files_queued for o in outcomes] == [2]
    assert read_pending(db_session) == []


def test_turning_it_back_on_queues_them_again(db_session, normal_user, seams):
    org = make_org(db_session)
    affected = build_world(db_session, normal_user, organization=org).file
    db_session.commit()
    _org_off(db_session, org, normal_user)
    run_label_resync(db_session)
    seams["reindex"].clear()

    policy_admin.apply_org_policy_update(
        db_session, org.id, normal_user.id, OrgPolicyUpdate(voiceprint_matching_enabled=True)
    )
    run_label_resync(db_session)

    assert seams["reindex"] == [affected.id]


def test_an_unchanged_patch_records_and_queues_nothing(db_session, normal_user, seams):
    org = make_org(db_session)
    build_world(db_session, normal_user, organization=org)
    db_session.commit()

    policy_admin.apply_org_policy_update(
        db_session, org.id, normal_user.id, OrgPolicyUpdate(voiceprint_matching_enabled=True)
    )
    policy_admin.apply_org_policy_update(
        db_session, org.id, normal_user.id, OrgPolicyUpdate(gender_inference_enabled=False)
    )

    assert read_pending(db_session) == []
    assert seams["tasks"] == 0
    assert run_label_resync(db_session) == []
    assert seams["reindex"] == []


def test_a_change_is_recorded_before_the_task_is_dispatched(db_session, normal_user, seams):
    org = make_org(db_session)
    build_world(db_session, normal_user, organization=org)
    db_session.commit()

    _org_off(db_session, org, normal_user)

    pending = read_pending(db_session)
    assert seams["tasks"] == 1
    assert [(p["kind"], p["organization_id"]) for p in pending] == [("organization", org.id)]
    assert pending[0]["from"] is True and pending[0]["to"] is False
    assert pending[0]["actor_user_id"] == normal_user.id


def test_a_request_for_a_scope_already_pending_replaces_it_instead_of_adding_one(
    db_session, normal_user, seams
):
    scope = ResyncScope("organization", organization_id=make_org(db_session).id)
    for to_value in (False, True):
        request_label_resync(
            db_session,
            scope,
            actor_user_id=normal_user.id,
            switch="organization_policy.voiceprint_matching_enabled",
            from_value=not to_value,
            to_value=to_value,
        )

    pending = read_pending(db_session)
    assert len(pending) == 1 and pending[0]["to"] is True


def test_a_pending_scope_survives_a_failed_dispatch_and_the_maintenance_arm_finishes_it(
    db_session, normal_user, seams
):
    from app.tasks.search_maintenance_task import _run_label_resync_arm

    org = make_org(db_session)
    build_world(db_session, normal_user, organization=org)
    build_world(db_session, normal_user, organization=org)
    db_session.commit()
    seams["outcome"] = ReindexDispatch.FAILED

    _org_off(db_session, org, normal_user)
    first = run_label_resync(db_session)

    assert [o.files_failed for o in first] == [2]
    assert len(read_pending(db_session)) == 1, "an unfinished scope must stay pending"

    seams["outcome"] = ReindexDispatch.QUEUED
    stats: dict[str, Any] = {}
    _run_label_resync_arm(db_session, stats)

    assert stats["label_resync_scopes"] == 1
    assert read_pending(db_session) == []


def test_a_request_made_while_a_pass_runs_is_not_retired_by_it(db_session, normal_user, seams):
    org = make_org(db_session)
    build_world(db_session, normal_user, organization=org)
    db_session.commit()
    scope = ResyncScope("organization", organization_id=org.id)

    def request(to_value: bool) -> None:
        request_label_resync(
            db_session,
            scope,
            actor_user_id=normal_user.id,
            switch="organization_policy.voiceprint_matching_enabled",
            from_value=not to_value,
            to_value=to_value,
        )

    request(False)

    def reindex_then_flip_again(file_id):
        request(True)  # the switch moves again while this pass is queueing
        return ReindexDispatch.QUEUED

    seams["outcome"] = reindex_then_flip_again
    run_label_resync(db_session)

    assert len(read_pending(db_session)) == 1, "the newer request must still be pending"


def test_the_audit_events_carry_the_exact_counts(db_session, normal_user, seams, audit):
    org = make_org(db_session)
    first = build_world(db_session, normal_user, organization=org).file
    build_world(db_session, normal_user, organization=org)
    _suggested_file(db_session, normal_user, org)
    db_session.commit()
    # One of the three is already queued within the debounce window.
    seams["outcome"] = lambda file_id: (
        ReindexDispatch.ALREADY_PENDING if file_id == first.id else ReindexDispatch.QUEUED
    )

    _org_off(db_session, org, normal_user)
    run_label_resync(db_session)

    requested, completed = _resync_events(audit)
    assert requested["details"]["phase"] == "requested"
    assert requested["user_id"] == normal_user.id
    assert requested["username"] == normal_user.email
    assert requested["organization_id"] == org.id
    assert requested["details"]["scope"] == "organization"
    assert requested["details"]["switch"] == "organization_policy.voiceprint_matching_enabled"
    assert (requested["details"]["from"], requested["details"]["to"]) == (True, False)
    assert completed["details"]["phase"] == "completed"
    assert completed["outcome"] == "success"
    assert completed["organization_id"] == org.id
    counts = {k: completed["details"][k] for k in completed["details"] if k.startswith("files_")}
    assert counts == {
        "files_selected": 3,
        "files_queued": 2,
        "files_already_pending": 1,
        "files_failed": 0,
    }
    assert completed["details"]["pages"] == 1
    assert set(completed["details"]) == {
        "phase",
        "scope",
        "organization_id",
        "user_id",
        "switch",
        "from",
        "to",
        "files_selected",
        "files_queued",
        "files_already_pending",
        "files_failed",
        "pages",
    }, "the event carries counts and the scope, never a file or a name"
    assert ALICE not in json.dumps(completed)


def test_an_incomplete_scope_is_audited_as_partial_with_its_failures(
    db_session, normal_user, seams, audit
):
    org = make_org(db_session)
    build_world(db_session, normal_user, organization=org)
    db_session.commit()
    seams["outcome"] = ReindexDispatch.FAILED

    _org_off(db_session, org, normal_user)
    run_label_resync(db_session)

    completed = _resync_events(audit)[-1]
    assert completed["details"]["phase"] == "incomplete"
    assert completed["outcome"] == "partial"
    assert completed["details"]["files_failed"] == 1
    assert completed["details"]["files_queued"] == 0


def test_a_user_switch_is_scoped_to_that_users_files(
    db_session, normal_user, other_user, seams, audit
):
    mine = build_world(db_session, normal_user).file
    theirs = build_world(db_session, other_user).file
    db_session.commit()

    policy_admin.write_user_switch(db_session, normal_user.id, False)
    run_label_resync(db_session)

    assert seams["reindex"] == [mine.id]
    assert theirs.id not in seams["reindex"]
    assert _resync_events(audit)[0]["details"]["scope"] == "user"
    assert _resync_events(audit)[0]["details"]["user_id"] == normal_user.id


def test_the_instance_switch_records_the_acting_administrator(
    db_session, normal_user, super_admin_user, seams, audit
):
    affected = build_world(db_session, normal_user).file
    db_session.commit()

    policy_admin.update_instance_policy(
        db_session,
        voiceprint_matching=False,
        gender_inference=None,
        actor_user_id=super_admin_user.id,
    )
    run_label_resync(db_session)

    requested, completed = _resync_events(audit)
    assert requested["user_id"] == super_admin_user.id
    assert requested["details"]["scope"] == "instance"
    assert completed["details"]["files_queued"] == 1
    assert seams["reindex"] == [affected.id]


# --- drift -----------------------------------------------------------------------------


def _drift(db, held: dict[str, set[str]], **kwargs):
    return detect_label_drift(db, indexed_labels=lambda uuids: held, **kwargs)


def test_drift_queues_a_file_whose_indexed_label_disagrees_with_the_policy(
    db_session, normal_user, seams, audit
):
    off = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    ).file
    on = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    ).file
    db_session.commit()
    # The index still holds Alice for both: right for `on`, stale for `off`.
    stale = {ALICE, "Carol Quillon", "Bob Brackett"}
    held = {str(off.uuid): set(stale), str(on.uuid): set(stale)}

    outcome = _drift(db_session, held)

    assert seams["reindex"] == [off.id]
    assert (outcome.drifted, outcome.queued) == (1, 1)
    drift_event = _resync_events(audit)[-1]
    assert drift_event["details"]["phase"] == "drift"
    assert drift_event["details"]["files_drifted"] == 1


def test_drift_ignores_files_the_index_agrees_with_or_does_not_hold(db_session, normal_user, seams):
    off = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    ).file
    unindexed = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    ).file
    db_session.commit()
    held = {
        str(off.uuid): {"SPEAKER_00", "Carol Quillon", "Bob Brackett"}
    }  # what the policy implies

    outcome = _drift(db_session, held)

    assert outcome.scanned == 2
    assert outcome.drifted == 0 and seams["reindex"] == []
    assert unindexed.id not in seams["reindex"]


def test_drift_resumes_from_its_cursor_and_wraps(db_session, normal_user, seams):
    org = make_org(db_session, voiceprints=False)
    files = [build_world(db_session, normal_user, organization=org).file for _ in range(3)]
    db_session.commit()
    held = {str(f.uuid): {ALICE} for f in files}
    mine = {f.id for f in files}

    first = _drift(db_session, held, limit=1)
    assert first.scanned >= 1
    cursor = (
        db_session.query(SystemSettings.value)
        .filter(SystemSettings.key == label_reindex.DRIFT_CURSOR_KEY)
        .scalar()
    )
    assert int(cursor) >= min(mine)

    while (
        int(
            db_session.query(SystemSettings.value)
            .filter(SystemSettings.key == label_reindex.DRIFT_CURSOR_KEY)
            .scalar()
        )
        != 0
    ):
        _drift(db_session, held, limit=1)

    assert mine <= set(seams["reindex"])


# --- registration ----------------------------------------------------------------------


def test_the_task_is_registered_included_and_routed_to_the_cpu_queue():
    import app.tasks.privacy_label_resync_task  # noqa: F401  (registers the task)

    assert "privacy.label_resync" in celery_app.tasks
    assert "app.tasks.privacy_label_resync_task" in celery_app.conf.include
    assert celery_app.conf.task_routes["privacy.label_resync"]["queue"] == CeleryQueues.CPU
