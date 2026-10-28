"""Privacy purge over HTTP and through the task body: tenant-scoped, confirmed, audited, two-pass.

A purge deletes biometric data for good, so the cases that matter are the ones that would delete
the wrong thing or delete without being asked: another tenant's or a personal workspace's rows and
OpenSearch documents (the delete query must carry the scope), a purge while the switch is still on
(the data would grow straight back), a typed confirmation that is not exactly ``PURGE``, a layer
switched back on between the request and the run, and a document written by something that raced
the switch-off (the second pass).

The OpenSearch client is a small in-memory evaluator of the exact query shapes the purge sends,
so a scope bug shows up as a surviving or wrongly deleted DOCUMENT, not only as a different body.
The task body runs in process against the savepoint-isolated session (its own sessions could not
see the test's uncommitted rows), with the broker call recorded.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core import constants
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerMatch
from app.models.media import SpeakerProfile
from app.models.media import Task
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from app.services.speaker_matching_service import SpeakerMatchingService
from app.tasks import privacy_purge_task as task_module
from app.tasks.privacy_purge_task import privacy_purge_task
from app.tasks.transcription.speaker_processor import create_or_get_speaker
from tests.fixtures.fake_speaker_index import FakeOpenSearch

ORG_PURGE = "/api/org-admin/policy/purge"
ME_PURGE = "/api/privacy/me/purge"
ADMIN_PURGE = "/api/admin/privacy-policy/purge"
BODY = {"targets": ["voiceprints", "inferred_gender"], "confirm": "PURGE"}


# --------------------------------------------------------------------------- #
# Stand-ins
# --------------------------------------------------------------------------- #


class _KeepOpen:
    """A session the purge may 'close': the test's savepoint-isolated one must stay open."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def opensearch(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    return fake


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


@pytest.fixture()
def reindexed(monkeypatch) -> list[tuple[int, str, int]]:
    queued: list[tuple[int, str, int]] = []

    def record(files):
        queued.extend(files)
        return 0

    monkeypatch.setattr(purge, "_queue_reindexes", record)
    return queued


@pytest.fixture()
def broker(monkeypatch) -> list[dict[str, Any]]:
    """Every ``apply_async`` of the purge task, recorded instead of published."""
    sent: list[dict[str, Any]] = []

    def record(args=None, kwargs=None, **options):
        sent.append({"kwargs": dict(kwargs or {}), **options})

    monkeypatch.setattr(privacy_purge_task, "apply_async", record)
    return sent


@pytest.fixture()
def in_process_task(monkeypatch, db_session):
    """Make the task body use the test's session, and return a runner for a recorded dispatch."""

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(task_module, "session_scope", scope)
    monkeypatch.setattr(task_module, "SessionLocal", lambda: _KeepOpen(db_session))

    def run(sent: dict[str, Any]) -> dict[str, Any]:
        result = privacy_purge_task.apply(kwargs=sent["kwargs"], task_id=sent["task_id"])
        outcome: dict[str, Any] = result.get(propagate=True)
        return outcome

    return run


@pytest.fixture(autouse=True)
def _capabilities():
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.defaults": True}
    )
    clear_write_backstop_cache()
    yield
    reset_capability_resolver()
    clear_write_backstop_cache()


# --------------------------------------------------------------------------- #
# Seeding: one scope's worth of voiceprint and gender data
# --------------------------------------------------------------------------- #


def document_type_of(doc_id: str) -> str | None:
    """The ``document_type`` the application stamps on a document with this ``_id``."""
    for prefix in ("profile", "cluster"):
        if doc_id.startswith(f"{prefix}_"):
            return prefix
    return None


def seed(gov, os_, owner, org, tag: str) -> SimpleNamespace:
    """Speakers, a cluster, a promoted cluster, a profile, a match and documents for one scope."""
    db = gov.db
    org_id = org.id if org is not None else None
    now = datetime.now(UTC)
    file_a = gov.file(owner, org=org if org is not None else None)
    file_b = gov.file(owner, org=org if org is not None else None)

    def speaker(media_file, name, **fields) -> Speaker:
        # Built by the helper transcription uses, so the row has exactly the columns production
        # gives it (``organization_id`` is NOT one of them: a speaker's tenant is its file's).
        row = create_or_get_speaker(db, owner.id, media_file.id, name)
        for column, value in fields.items():
            setattr(row, column, value)
        db.flush()
        return row

    guess = speaker(
        file_a,
        "SPEAKER_00",
        suggested_name=f"Ann {tag}",
        suggestion_source="voice_match",
        confidence=0.91,
        resolved_display_name=f"Ann {tag}",
        verified=False,
        predicted_gender="female",
        attribute_confidence={
            "gender": 0.9,
            "age_range": 0.7,
            "alignment": "match",
            "alignment_hint": "Ann",
            "metadata_hints": [{"name": "Ann", "role": "host", "confidence": 0.5, "source": "x"}],
        },
        attributes_predicted_at=now,
        predicted_age_range="adult",
    )
    other = speaker(file_b, "SPEAKER_00", predicted_gender="male", gender_confirmed_by_user=False)
    confirmed = speaker(
        file_b,
        "SPEAKER_01",
        display_name=f"Bea {tag}",
        verified=True,
        suggested_name=f"Bea {tag}",
        suggestion_source="voice_match",
        confidence=0.95,
        predicted_gender="female",
        gender_confirmed_by_user=True,
    )
    profile = SpeakerProfile(
        user_id=owner.id,
        organization_id=org_id,
        name=f"Profile {tag}",
        embedding_count=4,
        last_embedding_update=now,
        predicted_gender="female",
    )
    db.add(profile)
    db.flush()
    confirmed.profile_id = profile.id

    loose = SpeakerCluster(
        user_id=owner.id, organization_id=org_id, label=f"loose {tag}", member_count=2
    )
    named = SpeakerCluster(
        user_id=owner.id,
        organization_id=org_id,
        label=f"named {tag}",
        member_count=1,
        promoted_to_profile_id=profile.id,
    )
    db.add_all([loose, named])
    db.flush()
    for cluster, members in ((loose, (guess, other)), (named, (confirmed,))):
        for member in members:
            db.add(
                SpeakerClusterMember(cluster_id=cluster.id, speaker_id=member.id, confidence=0.9)
            )
            member.cluster_id = cluster.id
    first, second = sorted((guess.id, other.id))
    db.add(SpeakerMatch(speaker1_id=first, speaker2_id=second, confidence=0.9))
    db.commit()

    # One voiceprint document per speaker, the profile's, and each cluster's centroid, under the
    # ids the application writes them with.
    doc_ids = {
        *(str(row.uuid) for row in (guess, other, confirmed)),
        f"profile_{profile.uuid}",
        f"cluster_{loose.uuid}",
        f"cluster_{named.uuid}",
    }
    for doc_id in sorted(doc_ids):
        os_.add(doc_id, owner.id, org_id, document_type=document_type_of(doc_id))
    assert guess.organization_id is None, "speakers are not stamped with a tenant in production"
    return SimpleNamespace(
        tag=tag,
        owner=owner,
        org_id=org_id,
        files=(file_a, file_b),
        guess=guess.id,
        other=other.id,
        confirmed=confirmed.id,
        profile=profile.id,
        loose=loose.id,
        named=named.id,
        doc_ids=doc_ids,
    )


def turn_off(db, org, *, voiceprints=True, gender=True) -> None:
    row = db.get(OrganizationPolicy, org.id) or OrganizationPolicy(organization_id=org.id)
    if voiceprints:
        row.voiceprint_matching_enabled = False
    if gender:
        row.gender_inference_enabled = False
    db.add(row)
    db.commit()


def row_state(db, seeded) -> dict[str, Any]:
    db.expire_all()
    guess = db.get(Speaker, seeded.guess)
    other = db.get(Speaker, seeded.other)
    confirmed = db.get(Speaker, seeded.confirmed)
    profile = db.get(SpeakerProfile, seeded.profile)
    return {
        "guess": guess,
        "other": other,
        "confirmed": confirmed,
        "profile": profile,
        "loose_cluster": db.get(SpeakerCluster, seeded.loose),
        "named_cluster": db.get(SpeakerCluster, seeded.named),
        "matches": db.query(SpeakerMatch)
        .filter(SpeakerMatch.speaker1_id.in_([seeded.guess, seeded.other]))
        .count(),
        "loose_members": db.query(SpeakerClusterMember)
        .filter(SpeakerClusterMember.cluster_id == seeded.loose)
        .count(),
    }


def assert_untouched(db, seeded, os_) -> None:
    state = row_state(db, seeded)
    assert state["guess"].suggestion_source == "voice_match", f"{seeded.tag}: suggestion cleared"
    assert state["guess"].predicted_gender == "female", f"{seeded.tag}: gender cleared"
    assert state["profile"].embedding_count == 4, f"{seeded.tag}: profile embedding reset"
    assert state["loose_cluster"] is not None, f"{seeded.tag}: cluster deleted"
    assert state["matches"] == 1, f"{seeded.tag}: match deleted"
    assert seeded.doc_ids <= os_.ids(), f"{seeded.tag}: documents deleted"


def assert_purged(db, seeded, os_) -> None:
    state = row_state(db, seeded)
    guess, other, confirmed, profile = (
        state["guess"],
        state["other"],
        state["confirmed"],
        state["profile"],
    )
    # --- voiceprints: deleted
    assert state["matches"] == 0
    assert state["loose_cluster"] is None and state["loose_members"] == 0
    assert guess.cluster_id is None and other.cluster_id is None
    assert (profile.embedding_count, profile.last_embedding_update) == (0, None)
    assert (guess.suggested_name, guess.suggestion_source, guess.confidence) == (None, None, None)
    assert not (seeded.doc_ids & os_.ids())
    # --- inferred gender: deleted, together with its derived name-versus-voice comparison
    assert guess.predicted_gender is None and other.predicted_gender is None
    assert guess.attributes_predicted_at is None
    bag = guess.attribute_confidence
    assert "gender" not in bag and "alignment" not in bag and "alignment_hint" not in bag
    # --- kept: what a person asserted, and what is not about gender or voice
    assert bag["age_range"] == 0.7 and bag["metadata_hints"][0]["name"] == "Ann"
    assert guess.predicted_age_range == "adult"
    assert confirmed.verified is True and confirmed.display_name == f"Bea {seeded.tag}"
    # the voice comparison on a person-verified speaker goes too; the person's own word stays
    assert (confirmed.suggested_name, confirmed.suggestion_source) == (None, None)
    assert (confirmed.confidence, confirmed.suggestion_detail) == (None, None)
    assert confirmed.predicted_gender == "female" and confirmed.gender_confirmed_by_user is True
    assert confirmed.profile_id == profile.id
    assert profile.name == f"Profile {seeded.tag}"
    assert profile.predicted_gender == "female", "a profile's gender is only ever user-confirmed"
    assert state["named_cluster"] is not None and confirmed.cluster_id == seeded.named


def purge_via_api(client, gov, who, url, body=BODY, **acting):
    with gov.acting_as(who, **acting):
        return client.post(url, json=body)


# --------------------------------------------------------------------------- #
# Organization purge
# --------------------------------------------------------------------------- #


def test_an_org_purge_deletes_that_organization_and_only_that_organization(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    a = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    a2 = seed(gov, opensearch, gov.bob, gov.org, "orgA2")
    b = seed(gov, opensearch, gov.outsider, gov.other_org, "orgB")
    personal = seed(gov, opensearch, gov.alice, None, "personal")
    turn_off(gov.db, gov.org)

    response = purge_via_api(client, gov, gov.admin, ORG_PURGE)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    task_id = response.json()["task_id"]
    assert broker[0]["task_id"] == task_id and broker[0]["kwargs"]["phase"] == 1
    assert_untouched(gov.db, a, opensearch)  # nothing is deleted until the task runs

    result = in_process_task(broker[0])
    assert result["status"] == "completed"

    assert_purged(gov.db, a, opensearch)
    assert_purged(gov.db, a2, opensearch)
    assert_untouched(gov.db, b, opensearch)
    assert_untouched(gov.db, personal, opensearch)
    assert result["counts"]["opensearch"] == {
        "speaker_docs_deleted": 6,
        "profile_docs_deleted": 2,
        "cluster_docs_deleted": 4,
    }


def test_an_administrator_of_another_organization_cannot_purge_this_one(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    gov.join(gov.outsider, gov.other_org, role="org:admin")
    a = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    b = seed(gov, opensearch, gov.outsider, gov.other_org, "orgB")
    turn_off(gov.db, gov.org)  # A is off; B is still on
    turn_off(gov.db, gov.other_org, voiceprints=False, gender=False)

    # B's administrator: B's switches are still on, so their purge is refused.
    refused = purge_via_api(
        client, gov, gov.outsider, ORG_PURGE, org=gov.other_org, role="org:admin"
    )
    assert refused.status_code == status.HTTP_409_CONFLICT
    assert not broker

    # And once B is off, only B's data goes: A's switches being off does not extend to B's purge.
    turn_off(gov.db, gov.other_org)
    accepted = purge_via_api(
        client,
        gov,
        gov.outsider,
        ORG_PURGE,
        {"targets": ["voiceprints"], "confirm": "PURGE", "organization_id": gov.org.id},
        org=gov.other_org,
        role="org:admin",
    )
    assert accepted.status_code == status.HTTP_202_ACCEPTED
    in_process_task(broker[0])
    assert_untouched(gov.db, a, opensearch)
    assert not (b.doc_ids & opensearch.ids())


def test_a_purge_is_refused_while_the_switch_is_still_on(client, gov, opensearch, broker):
    a = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)  # voiceprints off, gender still on

    response = purge_via_api(client, gov, gov.admin, ORG_PURGE)
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == {
        "code": "policy_still_enabled",
        "targets": ["inferred_gender"],
    }
    assert not broker
    assert_untouched(gov.db, a, opensearch)
    assert not gov.db.query(Task).filter(Task.task_type == "privacy_purge").count()

    ok = purge_via_api(
        client, gov, gov.admin, ORG_PURGE, {"targets": ["voiceprints"], "confirm": "PURGE"}
    )
    assert ok.status_code == status.HTTP_202_ACCEPTED, "control: the target that is off is allowed"


@pytest.mark.parametrize(
    "body",
    [
        {"targets": ["voiceprints"], "confirm": "purge"},
        {"targets": ["voiceprints"], "confirm": "PURGE "},
        {"targets": ["voiceprints"]},
        {"targets": [], "confirm": "PURGE"},
        {"targets": ["everything"], "confirm": "PURGE"},
    ],
)
def test_without_the_exact_confirmation_nothing_is_queued(client, gov, opensearch, broker, body):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    response = purge_via_api(client, gov, gov.admin, ORG_PURGE, body)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert not broker and len(opensearch.docs) == 6


def test_a_member_and_an_anonymous_caller_cannot_purge(client, gov, opensearch, broker):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    assert purge_via_api(client, gov, gov.alice, ORG_PURGE).status_code == status.HTTP_403_FORBIDDEN
    assert client.post(ORG_PURGE, json=BODY).status_code == status.HTTP_401_UNAUTHORIZED
    assert not broker and len(opensearch.docs) == 6


# --------------------------------------------------------------------------- #
# The task
# --------------------------------------------------------------------------- #


def test_the_second_pass_catches_a_document_that_raced_the_switch(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    seeded = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    task_id = purge_via_api(client, gov, gov.admin, ORG_PURGE).json()["task_id"]

    first = in_process_task(broker[0])
    assert first["second_pass"] == "scheduled"
    second_dispatch = broker[1]
    assert second_dispatch["kwargs"]["phase"] == 2 and second_dispatch["task_id"] == task_id
    # long enough for the write backstop's cache plus an in-flight write
    assert second_dispatch["countdown"] > constants.PRIVACY_WRITE_BACKSTOP_TTL_SECONDS
    row = gov.db.get(Task, task_id)
    assert row.status == "in_progress", "the purge is not finished after pass 1"

    raced = str(gov.db.get(Speaker, seeded.guess).uuid)
    opensearch.add(raced, gov.alice.id, gov.org.id)  # a voiceprint written after pass 1 ran
    assert raced in opensearch.ids(), "control: the late write is there"
    result = in_process_task(second_dispatch)
    assert result["status"] == "completed"
    assert raced not in opensearch.ids()
    assert result["counts"]["opensearch"]["speaker_docs_deleted"] == 1
    gov.db.expire_all()
    assert gov.db.get(Task, task_id).status == "completed"


def test_the_task_aborts_if_the_switch_was_turned_back_on_before_it_ran(
    client, gov, opensearch, broker, in_process_task, events
):
    a = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    task_id = purge_via_api(client, gov, gov.admin, ORG_PURGE).json()["task_id"]

    row = gov.db.get(OrganizationPolicy, gov.org.id)
    row.voiceprint_matching_enabled = True  # the administrator changed their mind
    gov.db.commit()

    result = in_process_task(broker[0])
    assert result["status"] == "aborted" and result["reason"] == "policy_enabled"
    assert "voiceprints" in result["targets_still_enabled"]
    assert_untouched(gov.db, a, opensearch)
    gov.db.expire_all()
    assert gov.db.get(Task, task_id).status == "failed"
    aborted = [e for e in events if e["details"].get("phase", "").endswith("aborted")]
    assert aborted and all(e["outcome"].value == "failure" for e in aborted)


def test_an_unqueueable_purge_is_a_503_and_deletes_nothing(
    client, gov, opensearch, monkeypatch, events
):
    a = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)

    def refuse(*args, **kwargs):
        raise OSError("broker unreachable")

    monkeypatch.setattr(privacy_purge_task, "apply_async", refuse)
    response = purge_via_api(client, gov, gov.admin, ORG_PURGE)
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "broker" not in response.text, "no driver text on the wire"
    assert_untouched(gov.db, a, opensearch)
    rows = gov.db.query(Task).filter(Task.task_type == "privacy_purge").all()
    assert [r.status for r in rows] == ["failed"]
    assert any(e["details"].get("phase") == "not_queued" for e in events)


def test_a_store_that_could_not_be_purged_is_reported_partial_not_complete(
    client, gov, opensearch, broker, in_process_task, reindexed, events, monkeypatch
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)

    def failing(index, body, refresh=True, conflicts="proceed"):
        return {"deleted": 0, "failures": [{"cause": "x"}]}

    monkeypatch.setattr(opensearch, "delete_by_query", failing)
    purge_via_api(
        client, gov, gov.admin, ORG_PURGE, {"targets": ["voiceprints"], "confirm": "PURGE"}
    )
    in_process_task(broker[0])
    done = [e for e in events if e["details"].get("phase") == "pass_1_completed"]
    assert done and done[0]["outcome"].value == "partial"
    assert len(opensearch.docs) == 6, "the documents the store refused to delete are still there"


def test_a_cleared_suggestion_that_was_the_label_refreshes_the_label_and_the_index(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    a = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    purge_via_api(
        client, gov, gov.admin, ORG_PURGE, {"targets": ["voiceprints"], "confirm": "PURGE"}
    )
    in_process_task(broker[0])

    gov.db.expire_all()
    guess = gov.db.get(Speaker, a.guess)
    assert guess.resolved_display_name != "Ann orgA", (
        "the voiceprint-suggested name still labels it"
    )
    # every file with a speaker that lost a suggestion is queued, the person-verified speaker's
    # included (its label is unchanged, the rebuild is then a no-op)
    assert sorted(f[1] for f in reindexed) == sorted(str(f.uuid) for f in a.files), (
        "the chunk index is not refreshed"
    )


def test_the_policy_is_fail_closed_when_it_cannot_be_read(gov, monkeypatch):
    turn_off(gov.db, gov.org)
    scope = purge.PurgeScope("organization", organization_id=gov.org.id)
    assert purge.targets_still_enabled(gov.db, scope, ["voiceprints"]) == []  # control

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr("app.services.privacy.policy._stored_layers", broken)
    assert purge.targets_still_enabled(gov.db, scope, ["voiceprints"]) == ["voiceprints"]


def test_a_scope_cannot_be_built_without_what_it_needs():
    with pytest.raises(ValueError):
        purge.PurgeScope("organization")
    with pytest.raises(ValueError):
        purge.PurgeScope("user")
    with pytest.raises(ValueError):
        purge.PurgeScope("instance_all", organization_id=1)
    with pytest.raises(ValueError):
        purge.PurgeScope("everyone")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_every_purge_is_audited_with_counts_and_no_names(
    client, gov, opensearch, broker, in_process_task, reindexed, events
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    purge_via_api(client, gov, gov.admin, ORG_PURGE)
    in_process_task(broker[0])

    by_type: dict[Any, list[dict]] = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e)
    for event_type in (
        AuditEventType.PRIVACY_VOICEPRINT_PURGE,
        AuditEventType.PRIVACY_GENDER_PURGE,
    ):
        phases = [e["details"]["phase"] for e in by_type[event_type]]
        assert phases == ["requested", "pass_1_completed"], event_type
        for e in by_type[event_type]:
            assert e["organization_id"] == gov.org.id
            assert e["user_id"] == gov.admin.id
            assert e["details"]["scope"] == {
                "kind": "organization",
                "organization_id": gov.org.id,
                "user_id": None,
            }
    completed = by_type[AuditEventType.PRIVACY_VOICEPRINT_PURGE][1]
    assert sum(completed["details"]["counts"]["opensearch"].values()) == 6
    assert "Ann" not in repr(events) and gov.alice.email not in repr(events)


# --------------------------------------------------------------------------- #
# The person's own purge
# --------------------------------------------------------------------------- #


def test_a_person_purges_only_their_own_data_in_the_active_scope(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    mine = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    mine_personal = seed(gov, opensearch, gov.alice, None, "alice-personal")
    bobs = seed(gov, opensearch, gov.bob, gov.org, "bob-org")
    gov.db.add(
        UserSetting(
            user_id=gov.alice.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    gov.db.add(
        UserSetting(
            user_id=gov.alice.id,
            setting_key=constants.PRIVACY_USER_GENDER_DETECTION_KEY,
            setting_value="false",
        )
    )
    gov.db.commit()

    response = purge_via_api(client, gov, gov.alice, ME_PURGE)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    in_process_task(broker[0])

    assert_purged(gov.db, mine, opensearch)
    assert_untouched(gov.db, bobs, opensearch)
    assert_untouched(gov.db, mine_personal, opensearch)


def _switch_off_for(db, user) -> None:
    for key in (
        constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        constants.PRIVACY_USER_GENDER_DETECTION_KEY,
    ):
        db.add(UserSetting(user_id=user.id, setting_key=key, setting_value="false"))
    db.commit()


def test_the_personal_workspace_purge_leaves_the_same_persons_organization_data(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    mine_personal = seed(gov, opensearch, gov.alice, None, "alice-personal")
    mine_org = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    others_personal = seed(gov, opensearch, gov.bob, None, "bob-personal")
    _switch_off_for(gov.db, gov.alice)

    response = purge_via_api(client, gov, gov.alice, ME_PURGE, org=None)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    result = in_process_task(broker[0])

    assert_purged(gov.db, mine_personal, opensearch)
    assert_untouched(gov.db, mine_org, opensearch)
    assert_untouched(gov.db, others_personal, opensearch)
    # Only the personal workspace's rows were counted: 2 speakers carry inferred gender, 1 match,
    # 2 voiceprint suggestions (one on a person-verified speaker).
    postgres = result["counts"]["postgres"]
    assert postgres["gender_speakers_cleared"] == 2
    assert postgres["speaker_matches_deleted"] == 1
    assert postgres["suggestions_cleared"] == 2


# One store at a time: what each of the four stores holds for a seeded scope, as "kept?" Each
# reader returns True while the scope's rows / documents are still there.
_STORES = {
    "inferred_gender": lambda db, s, os_: (
        db.get(Speaker, s.guess).predicted_gender is not None
        and db.get(Speaker, s.other).predicted_gender is not None
    ),
    "voice_suggestions": lambda db, s, os_: db.get(Speaker, s.guess).suggestion_source is not None,
    "speaker_matches": lambda db, s, os_: (
        db.query(SpeakerMatch).filter(SpeakerMatch.speaker1_id.in_([s.guess, s.other])).count() == 1
    ),
    "opensearch_voiceprints": lambda db, s, os_: s.doc_ids <= os_.ids(),
}


@pytest.mark.parametrize("store", sorted(_STORES))
@pytest.mark.parametrize("purged_scope", ["organization", "personal"])
def test_each_purge_touches_only_its_own_tenants_rows_in_each_store(
    client, gov, opensearch, broker, in_process_task, reindexed, store, purged_scope
):
    """Two tenants hold the SAME person's data side by side; a purge of one leaves the other.

    The organization purge and the person's personal-workspace purge are the two that could
    reach across: both are run against the identical pair, and each store is read separately so
    a failure names the store that was crossed.
    """
    in_org = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    other_org_member = seed(gov, opensearch, gov.bob, gov.org, "bob-org")
    personal = seed(gov, opensearch, gov.alice, None, "alice-personal")
    elsewhere = seed(gov, opensearch, gov.outsider, gov.other_org, "outsider-org")
    if purged_scope == "organization":
        turn_off(gov.db, gov.org)
        response = purge_via_api(client, gov, gov.admin, ORG_PURGE)
        gone, kept = [in_org, other_org_member], [personal, elsewhere]
    else:
        _switch_off_for(gov.db, gov.alice)
        response = purge_via_api(client, gov, gov.alice, ME_PURGE, org=None)
        gone, kept = [personal], [in_org, other_org_member, elsewhere]
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    gov.db.expire_all()
    for seeded in gone + kept:
        assert _STORES[store](gov.db, seeded, opensearch), f"{seeded.tag}: seeded wrongly"

    in_process_task(broker[0])

    gov.db.expire_all()
    assert not any(_STORES[store](gov.db, seeded, opensearch) for seeded in gone)
    assert all(_STORES[store](gov.db, seeded, opensearch) for seeded in kept)


def test_an_organization_purge_counts_the_organizations_speakers_not_zero(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    seed(gov, opensearch, gov.bob, gov.org, "orgA2")
    seed(gov, opensearch, gov.alice, None, "personal")
    turn_off(gov.db, gov.org)

    purge_via_api(client, gov, gov.admin, ORG_PURGE)
    result = in_process_task(broker[0])

    # Two seeds in the organization, each with two inferred-gender speakers, a match and a
    # voiceprint suggestion on two speakers (one of them person-verified); the personal
    # workspace's identical rows are not in these numbers.
    postgres = result["counts"]["postgres"]
    assert postgres["gender_speakers_cleared"] == 4
    assert postgres["speaker_matches_deleted"] == 2
    assert postgres["suggestions_cleared"] == 4


def test_a_person_may_purge_once_their_organization_turned_it_off_without_changing_their_switch(
    client, gov, opensearch, broker
):
    seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    turn_off(gov.db, gov.org)
    # alice's own switch is untouched (on), and the organization now locks it
    with gov.acting_as(gov.alice):
        assert (
            client.put("/api/privacy/me", json={"voiceprint_matching_enabled": False}).status_code
            == status.HTTP_409_CONFLICT
        )
    response = purge_via_api(client, gov, gov.alice, ME_PURGE)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text


def test_a_person_cannot_purge_while_everything_is_on(client, gov, opensearch, broker):
    mine = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    response = purge_via_api(client, gov, gov.alice, ME_PURGE)
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"]["code"] == "policy_still_enabled"
    assert not broker
    assert_untouched(gov.db, mine, opensearch)


# --------------------------------------------------------------------------- #
# Instance purge
# --------------------------------------------------------------------------- #


def _instance_off(client, headers) -> None:
    response = client.put(
        "/api/admin/privacy-policy",
        headers=headers,
        json={"voiceprint_matching_enabled": False, "gender_inference_enabled": False},
    )
    assert response.status_code == status.HTTP_200_OK, response.text


def test_an_instance_purge_of_the_personal_scope_leaves_every_tenant(
    client, gov, super_admin_token_headers, opensearch, broker, in_process_task, reindexed
):
    personal = seed(gov, opensearch, gov.alice, None, "personal")
    personal2 = seed(gov, opensearch, gov.outsider, None, "personal2")
    tenant = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    _instance_off(client, super_admin_token_headers)

    response = client.post(
        ADMIN_PURGE, headers=super_admin_token_headers, json={**BODY, "scope": "personal"}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    in_process_task(broker[0])

    assert_purged(gov.db, personal, opensearch)
    assert_purged(gov.db, personal2, opensearch)
    assert_untouched(gov.db, tenant, opensearch)


def test_scope_all_is_refused_in_a_multi_tenant_deployment(
    client, gov, super_admin_token_headers, opensearch, broker, monkeypatch
):
    from app.services import platform_access

    tenant = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    _instance_off(client, super_admin_token_headers)
    body = {**BODY, "scope": "all"}

    monkeypatch.setattr(platform_access.settings, "TENANCY_MODE", "multi")
    refused = client.post(ADMIN_PURGE, headers=super_admin_token_headers, json=body)
    assert refused.status_code == status.HTTP_403_FORBIDDEN
    assert refused.json()["detail"] == {"code": "purge_scope_not_allowed"}
    assert not broker
    assert_untouched(gov.db, tenant, opensearch)

    monkeypatch.setattr(platform_access.settings, "TENANCY_MODE", "single")
    allowed = client.post(ADMIN_PURGE, headers=super_admin_token_headers, json=body)
    assert allowed.status_code == status.HTTP_202_ACCEPTED, "control: single-tenant mode allows it"


def test_scope_all_deletes_everything_when_allowed(
    client,
    gov,
    super_admin_token_headers,
    opensearch,
    broker,
    in_process_task,
    reindexed,
    monkeypatch,
):
    from app.services import platform_access

    monkeypatch.setattr(platform_access.settings, "TENANCY_MODE", "single")
    everything = [
        seed(gov, opensearch, gov.alice, None, "personal"),
        seed(gov, opensearch, gov.alice, gov.org, "orgA"),
        seed(gov, opensearch, gov.outsider, gov.other_org, "orgB"),
    ]
    _instance_off(client, super_admin_token_headers)
    client.post(ADMIN_PURGE, headers=super_admin_token_headers, json={**BODY, "scope": "all"})
    in_process_task(broker[0])
    for seeded in everything:
        assert_purged(gov.db, seeded, opensearch)


def test_an_instance_purge_is_refused_while_the_instance_switch_is_on(
    client, gov, super_admin_token_headers, opensearch, broker
):
    personal = seed(gov, opensearch, gov.alice, None, "personal")
    response = client.post(
        ADMIN_PURGE, headers=super_admin_token_headers, json={**BODY, "scope": "personal"}
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert not broker
    assert_untouched(gov.db, personal, opensearch)


def test_only_a_super_admin_can_start_an_instance_purge(
    client, user_token_headers, admin_token_headers, opensearch, broker
):
    body = {**BODY, "scope": "personal"}
    anonymous = client.post(ADMIN_PURGE, json=body)
    assert anonymous.status_code == status.HTTP_403_FORBIDDEN
    assert (
        client.post(ADMIN_PURGE, headers=user_token_headers, json=body).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert (
        client.post(ADMIN_PURGE, headers=admin_token_headers, json=body).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert not broker


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def test_only_the_initiator_can_read_a_purge_status(
    client, gov, opensearch, broker, in_process_task, reindexed, monkeypatch
):
    store: dict[str, str] = {}
    fake_redis = SimpleNamespace(
        set=lambda key, value, ex=None: store.__setitem__(key, value),
        get=lambda key: store.get(key),
    )
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake_redis)
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    task_id = purge_via_api(client, gov, gov.admin, ORG_PURGE).json()["task_id"]
    url = f"/api/privacy/purge/{task_id}"

    with gov.acting_as(gov.admin):
        pending = client.get(url).json()
    assert pending == {
        "task_id": task_id,
        "status": "pending",
        "counts": None,
        "residual": None,
        "residual_ok": None,
        "residual_unverified": [],
        "audit": [],
    }

    in_process_task(broker[0])
    in_process_task(broker[1])
    with gov.acting_as(gov.admin):
        done = client.get(url).json()
    assert done["status"] == "completed"
    assert sum(done["counts"]["opensearch"].values()) == 6
    assert done["residual_ok"] is True

    with gov.acting_as(gov.alice):
        assert client.get(url).status_code == status.HTTP_404_NOT_FOUND
    with gov.acting_as(gov.admin):
        assert client.get("/api/privacy/purge/not-a-task").status_code == status.HTTP_404_NOT_FOUND
    assert client.get(url).status_code == status.HTTP_401_UNAUTHORIZED


# --------------------------------------------------------------------------- #
# Names and links a voiceprint made are reverted; a person's are kept (#1280)
# --------------------------------------------------------------------------- #


def make_speaker_row(gov, owner, org, label: str, **fields) -> Speaker:
    """A speaker in a fresh file of ``org`` (None = personal), built as diarization builds it."""
    media_file = gov.file(owner, org=org)
    row = create_or_get_speaker(gov.db, owner.id, media_file.id, label)
    for column, value in fields.items():
        setattr(row, column, value)
    gov.db.commit()
    return row


def make_profile_row(gov, owner, org, name: str) -> SpeakerProfile:
    profile = SpeakerProfile(
        user_id=owner.id, organization_id=org.id if org is not None else None, name=name
    )
    gov.db.add(profile)
    gov.db.commit()
    return profile


def auto_accept_through_the_matcher(gov, owner, speaker: Speaker, profile: SpeakerProfile) -> None:
    """Make ``speaker`` a voiceprint-named one the way upload-time matching does."""
    match = {
        "confidence": 0.93,
        "suggested_name": profile.name,
        "auto_accept": True,
        "profile_id": profile.id,
        "profile_uuid": str(profile.uuid),
    }
    with (
        patch("app.services.speaker_matching_service.add_speaker_embedding"),
        patch("app.services.opensearch_service.update_speaker_profile"),
        patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        patch.object(SpeakerMatchingService, "_propagate_profile_assignment"),
        patch.object(SpeakerMatchingService, "match_speaker_to_known_speakers", return_value=match),
        patch("app.tasks.rename_propagation_task.propagate_speaker_rename.delay"),
    ):
        SpeakerMatchingService(gov.db, None).process_speaker_embeddings_native(
            media_file_id=int(speaker.media_file_id),
            user_id=int(owner.id),
            native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
        )
    gov.db.expire_all()


def run_org_purge(client, gov, broker, in_process_task, targets=("voiceprints",)):
    body = {"targets": list(targets), "confirm": "PURGE"}
    response = purge_via_api(client, gov, gov.admin, ORG_PURGE, body)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    return response.json()["task_id"], in_process_task(broker[-1])


def test_a_purge_removes_an_auto_accepted_name_and_link(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Alice")
    speaker = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    auto_accept_through_the_matcher(gov, gov.alice, speaker, profile)
    row = gov.db.get(Speaker, speaker.id)
    assert (row.display_name, row.profile_id, row.verified) == ("Alice", profile.id, True)
    assert row.identity_source == "voiceprint", "control: the matcher recorded its authorship"
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    row = gov.db.get(Speaker, speaker.id)
    assert (row.display_name, row.profile_id, row.verified, row.identity_source) == (
        None,
        None,
        False,
        None,
    )
    assert row.resolved_display_name == "SPEAKER_00", "the label is the diarizer's again"
    assert result["counts"]["postgres"]["speaker_identities_reverted"] == 1
    assert [f[1] for f in reindexed] == [str(speaker.media_file.uuid)], "its file is reindexed"
    assert gov.db.get(SpeakerProfile, profile.id).name == "Alice", "the profile itself stays"


def test_a_purge_keeps_a_typed_name_and_removes_only_a_machine_link(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Zed")
    typed = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Zed",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint_link",
    )
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    row = gov.db.get(Speaker, typed.id)
    assert (row.display_name, row.verified, row.profile_id) == ("Zed", True, None)
    assert row.identity_source is None
    assert result["counts"]["postgres"]["speaker_links_removed"] == 1
    assert result["counts"]["postgres"]["speaker_identities_reverted"] == 0


def test_a_purge_never_touches_a_human_or_unknown_identity(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Bea")
    kept = {
        source: make_speaker_row(
            gov,
            gov.alice,
            gov.org,
            "SPEAKER_00",
            display_name="Bea",
            verified=True,
            profile_id=profile.id,
            identity_source=source,
        )
        for source in ("human", None)
    }
    machine = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Bea",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
    )
    turn_off(gov.db, gov.org, gender=False)

    run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    for source, row in kept.items():
        fresh = gov.db.get(Speaker, row.id)
        assert (fresh.display_name, fresh.verified, fresh.profile_id) == ("Bea", True, profile.id)
        assert fresh.identity_source == source
    assert gov.db.get(Speaker, machine.id).display_name is None, "control: the machine one went"


def test_a_purge_in_one_organization_leaves_another_organizations_machine_identities(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    mine = make_speaker_row(
        gov, gov.alice, gov.org, "SPEAKER_00", display_name="Ann", identity_source="voiceprint"
    )
    theirs = make_speaker_row(
        gov,
        gov.outsider,
        gov.other_org,
        "SPEAKER_00",
        display_name="Ann",
        identity_source="voiceprint",
    )
    personal = make_speaker_row(
        gov, gov.alice, None, "SPEAKER_00", display_name="Ann", identity_source="voiceprint"
    )
    turn_off(gov.db, gov.org, gender=False)

    run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    assert gov.db.get(Speaker, mine.id).display_name is None
    for other in (theirs, personal):
        row = gov.db.get(Speaker, other.id)
        assert (row.display_name, row.identity_source) == ("Ann", "voiceprint")


def test_a_purge_takes_machine_members_out_of_a_promoted_cluster(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Ann")
    machine = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
    )
    human = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        profile_id=profile.id,
        identity_source="human",
    )
    solo_machine = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
    )

    def promoted(label, *members):
        cluster = SpeakerCluster(
            user_id=gov.alice.id,
            organization_id=gov.org.id,
            label=label,
            member_count=len(members),
            promoted_to_profile_id=profile.id,
        )
        gov.db.add(cluster)
        gov.db.flush()
        for member in members:
            gov.db.add(SpeakerClusterMember(cluster_id=cluster.id, speaker_id=member.id))
            member.cluster_id = cluster.id
        gov.db.commit()
        return cluster

    mixed_id = promoted("mixed", machine, human).id
    only_machine_id = promoted("only-machine", solo_machine).id
    machine_id, human_id = machine.id, human.id
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    members = {
        m.speaker_id
        for m in gov.db.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.cluster_id == mixed_id
        )
    }
    assert members == {human_id}, "the machine member left; the human one stayed"
    assert gov.db.get(SpeakerCluster, mixed_id).member_count == 1
    assert gov.db.get(Speaker, machine_id).cluster_id is None
    assert gov.db.get(Speaker, human_id).cluster_id == mixed_id
    assert gov.db.get(SpeakerCluster, only_machine_id) is None, "an emptied cluster is deleted"
    postgres = result["counts"]["postgres"]
    assert postgres["promoted_cluster_members_removed"] == 2
    assert postgres["promoted_clusters_deleted"] == 1


def test_a_cross_scope_match_is_deleted_and_counted_and_the_other_speaker_is_untouched(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    # Created first so its id is LOWER than the in-scope speakers' (it becomes ``speaker1``),
    # and last so it is HIGHER: both sides of the pair table are exercised.
    outside_low = make_speaker_row(
        gov,
        gov.outsider,
        gov.other_org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        identity_source="voiceprint",
    )
    inside_a = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    inside_b = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    outside_high = make_speaker_row(gov, gov.outsider, gov.other_org, "SPEAKER_00")

    def match(first, second):
        low, high = sorted((first.id, second.id))
        gov.db.add(SpeakerMatch(speaker1_id=low, speaker2_id=high, confidence=0.9))

    match(inside_a, inside_b)  # both sides in scope
    match(outside_low, inside_a)  # the in-scope speaker is speaker2
    match(inside_b, outside_high)  # the in-scope speaker is speaker1
    match(outside_low, outside_high)  # entirely another tenant's
    gov.db.commit()
    kept = (outside_low.id, outside_high.id)
    survivor_id = outside_low.id
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    postgres = result["counts"]["postgres"]
    assert postgres["speaker_matches_deleted"] == 1
    assert postgres["cross_scope_matches"] == 2
    remaining = [
        (m.speaker1_id, m.speaker2_id)
        for m in gov.db.query(SpeakerMatch).filter(SpeakerMatch.speaker1_id.in_(kept))
    ]
    assert remaining == [tuple(sorted(kept))]
    survivor = gov.db.get(Speaker, survivor_id)
    assert (survivor.display_name, survivor.identity_source, survivor.verified) == (
        "Ann",
        "voiceprint",
        True,
    ), "the other scope's speaker row is never modified"


def test_a_voiceprint_suggestion_goes_with_the_identity_it_made_and_an_llm_one_stays(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    voice = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        identity_source="voiceprint",
        suggested_name="Ann",
        suggestion_source="profile_match",
        confidence=0.9,
    )
    llm = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Ann",
        verified=True,
        identity_source="voiceprint",
        suggested_name="Anne",
        suggestion_source="llm_analysis",
        confidence=0.8,
    )
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    gov.db.expire_all()
    cleared = gov.db.get(Speaker, voice.id)
    assert (cleared.suggested_name, cleared.suggestion_source, cleared.confidence) == (
        None,
        None,
        None,
    )
    kept = gov.db.get(Speaker, llm.id)
    assert (kept.suggested_name, kept.suggestion_source, kept.confidence) == (
        "Anne",
        "llm_analysis",
        0.8,
    )
    assert result["counts"]["postgres"]["suggestions_cleared"] == 1


def test_a_clean_purge_reports_an_all_zero_residual(
    client, gov, opensearch, broker, in_process_task, reindexed, events
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)

    task_id, _result = run_org_purge(
        client, gov, broker, in_process_task, ("voiceprints", "inferred_gender")
    )

    completed = [e for e in events if e["details"].get("phase") == "pass_1_completed"]
    assert len(completed) == 2
    for event in completed:
        details = event["details"]
        assert details["residual_ok"] is True and details["residual_unverified"] == []
        assert all(v == 0 for v in details["residual"].values()), details["residual"]
    voiceprints = next(
        e["details"]
        for e in completed
        if e["event_type"] == AuditEventType.PRIVACY_VOICEPRINT_PURGE
    )
    assert set(voiceprints["residual"]) == set(purge.VOICEPRINT_RESIDUAL_KEYS)
    assert task_id


def test_a_residual_makes_the_response_and_the_audit_event_report_failure(
    client, gov, opensearch, broker, in_process_task, reindexed, events, monkeypatch
):
    """A voiceprint that survives the purge (here: the store silently skips one delete)."""
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    survivor = next(doc["id"] for doc in opensearch.docs if doc["id"].startswith("profile_"))
    real_delete = opensearch.delete_by_query

    def skipping(index, body, refresh=True, conflicts="proceed"):
        saved = [d for d in opensearch.docs if d["id"] == survivor]
        result = real_delete(index, body, refresh, conflicts)
        if saved and survivor not in opensearch.ids():
            opensearch.docs.extend(saved)  # the store reported success and kept the document
            result["deleted"] -= 1
        return result

    monkeypatch.setattr(opensearch, "delete_by_query", skipping)
    store: dict[str, str] = {}
    monkeypatch.setattr(
        "app.core.redis.get_redis",
        lambda: SimpleNamespace(
            set=lambda key, value, ex=None: store.__setitem__(key, value), get=store.get
        ),
    )
    body = {"targets": ["voiceprints"], "confirm": "PURGE"}
    task_id = purge_via_api(client, gov, gov.admin, ORG_PURGE, body).json()["task_id"]

    first = in_process_task(broker[0])
    assert first["status"] == "completed", "pass 1 records the residual and leaves pass 2 to try"
    final = in_process_task(broker[1])

    assert final["status"] == "failed" and final["reason"] == "residual"
    assert final["residual"]["profile_docs"] == 1
    gov.db.expire_all()
    assert gov.db.get(Task, task_id).status == "failed"
    finals = [e for e in events if e["details"].get("phase") == "pass_2_completed"]
    assert finals and finals[0]["outcome"].value == "failure"
    assert finals[0]["details"]["reason"] == "residual"
    assert finals[0]["details"]["residual_ok"] is False
    assert finals[0]["details"]["residual"]["profile_docs"] == 1
    with gov.acting_as(gov.admin):
        shown = client.get(f"/api/privacy/purge/{task_id}").json()
    assert shown["status"] == "failed"
    assert shown["residual_ok"] is False and shown["residual"]["profile_docs"] == 1
    assert shown["audit"] == [{"event_type": "privacy.voiceprints.purge", "task_id": task_id}]


def test_a_store_that_cannot_be_measured_is_unverified_not_zero(
    client, gov, opensearch, broker, in_process_task, reindexed, events, monkeypatch
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)

    def unreachable(index, body):
        raise ConnectionError("cluster down")

    monkeypatch.setattr(opensearch, "count", unreachable, raising=False)
    body = {"targets": ["voiceprints"], "confirm": "PURGE"}
    purge_via_api(client, gov, gov.admin, ORG_PURGE, body)

    in_process_task(broker[0])

    done = next(e for e in events if e["details"].get("phase") == "pass_1_completed")
    details = done["details"]
    assert details["residual_ok"] is False
    assert details["residual_unverified"] == ["cluster_docs", "profile_docs", "speaker_docs"]
    assert "speaker_docs" not in details["residual"]
    assert done["outcome"].value != "success"


def test_the_audit_record_carries_actor_scope_targets_counts_and_residual(
    client, gov, opensearch, broker, in_process_task, reindexed, events
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    task_id, _ = run_org_purge(
        client, gov, broker, in_process_task, ("voiceprints", "inferred_gender")
    )

    voiceprints = next(
        e
        for e in events
        if e["event_type"] == AuditEventType.PRIVACY_VOICEPRINT_PURGE
        and e["details"].get("phase") == "pass_1_completed"
    )
    assert voiceprints["user_id"] == gov.admin.id and voiceprints["username"] == gov.admin.email
    assert voiceprints["organization_id"] == gov.org.id
    details = voiceprints["details"]
    assert details["task_id"] == task_id
    assert details["targets"] == ["voiceprints", "inferred_gender"]
    assert details["scope"] == {
        "kind": "organization",
        "organization_id": gov.org.id,
        "user_id": None,
    }
    assert set(details["counts"]) == {"postgres", "opensearch"}
    assert details["counts"]["opensearch"] == {
        "speaker_docs_deleted": 3,
        "profile_docs_deleted": 1,
        "cluster_docs_deleted": 2,
    }
    assert details["counts"]["postgres"]["speaker_matches_deleted"] == 1
    assert details["counts"]["postgres"]["clusters_deleted"] == 1
    assert details["residual_ok"] is True
    gender = next(
        e
        for e in events
        if e["event_type"] == AuditEventType.PRIVACY_GENDER_PURGE
        and e["details"].get("phase") == "pass_1_completed"
    )
    assert gender["details"]["counts"]["postgres"] == {"gender_speakers_cleared": 2}
    assert gender["details"]["residual"] == {"speakers_with_inferred_gender": 0}


# --------------------------------------------------------------------------- #
# A cluster centroid document pass 1 could not delete is finished by pass 2
#
# A pass deletes the cluster ROWS from Postgres and then their ``cluster_<uuid>`` documents. When
# the document delete fails, pass 2 recomputes its targets from Postgres, finds no such cluster
# and would neither delete nor measure the document: a clean residual with a voiceprint centroid
# still indexed. Pass 1 therefore hands pass 2 the ids it could not confirm gone.
# --------------------------------------------------------------------------- #

VOICEPRINTS_ONLY = {"targets": ["voiceprints"], "confirm": "PURGE"}


@pytest.fixture
def status_record(monkeypatch):
    """The purge's Redis status record, as a dict."""
    store: dict[str, str] = {}
    monkeypatch.setattr(
        "app.core.redis.get_redis",
        lambda: SimpleNamespace(
            set=lambda key, value, ex=None: store.__setitem__(key, value), get=store.get
        ),
    )


def _fail_deletes_of(opensearch, document_id: str, *, while_active) -> None:
    real = opensearch.delete_by_query

    def flaky(index, body, refresh=True, conflicts="proceed"):
        wanted = body.get("query", {}).get("ids", {}).get("values", [])
        if while_active() and document_id in wanted:
            raise ConnectionError("transient index failure")
        return real(index, body, refresh, conflicts)

    opensearch.delete_by_query = flaky


def _loose_cluster_document(gov, world) -> str:
    return f"cluster_{gov.db.get(SpeakerCluster, world.loose).uuid}"


def _request_purge(client, gov, opensearch, document: str, scope: str):
    """Ask for the purge as ``scope``; an organization purge's document is unstamped (legacy).

    An organization purge also sweeps its own stamp, which would delete a stamped document
    whether or not the id-based delete worked; the unstamped legacy document is the one only the
    ids reach, as every document of a user's purge is.
    """
    if scope == "organization":
        for doc in opensearch.docs:
            if doc["id"] == document:
                doc.pop("organization_id")
        return purge_via_api(client, gov, gov.admin, ORG_PURGE, VOICEPRINTS_ONLY)
    return purge_via_api(client, gov, gov.alice, ME_PURGE, VOICEPRINTS_ONLY)


@pytest.mark.usefixtures("status_record")
@pytest.mark.parametrize("scope", ["user", "organization"])
def test_a_centroid_document_pass_one_could_not_delete_is_deleted_and_measured_by_pass_two(
    client, gov, opensearch, broker, in_process_task, reindexed, events, scope
):
    world = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    document = _loose_cluster_document(gov, world)
    in_pass_one = {"yes": True}
    _fail_deletes_of(opensearch, document, while_active=lambda: in_pass_one["yes"])
    _request_purge(client, gov, opensearch, document, scope)

    first = in_process_task(broker[0])
    in_pass_one["yes"] = False
    final = in_process_task(broker[1])

    assert first["second_pass"] == "scheduled"
    assert document.removeprefix("cluster_") in broker[1]["kwargs"]["carried_cluster_uuids"]
    assert document not in opensearch.ids(), "pass two retried the document pass one left"
    assert final["status"] == "completed"
    finished = [e for e in events if e["details"].get("phase") == "pass_2_completed"]
    assert finished[0]["outcome"].value == "success"
    assert finished[0]["details"]["residual_ok"] is True


@pytest.mark.usefixtures("status_record")
@pytest.mark.parametrize("scope", ["user", "organization"])
def test_a_centroid_document_that_never_deletes_fails_the_purge_it_is_never_complete(
    client, gov, opensearch, broker, in_process_task, reindexed, events, scope
):
    world = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    document = _loose_cluster_document(gov, world)
    _fail_deletes_of(opensearch, document, while_active=lambda: True)
    _request_purge(client, gov, opensearch, document, scope)

    in_process_task(broker[0])
    final = in_process_task(broker[1])

    assert document in opensearch.ids()
    assert final["status"] == "failed" and final["reason"] == "residual"
    assert final["residual"]["cluster_docs"] >= 1, "pass two still measures the carried id"
    finished = [e for e in events if e["details"].get("phase") == "pass_2_completed"]
    assert finished[0]["outcome"].value == "failure"


@pytest.mark.usefixtures("status_record")
def test_a_clean_first_pass_carries_nothing_to_the_second(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    purge_via_api(client, gov, gov.alice, ME_PURGE, VOICEPRINTS_ONLY)

    in_process_task(broker[0])

    assert broker[1]["kwargs"]["carried_cluster_uuids"] == []


# --- the record of how an identity or a suggestion came to be (#1280 follow-up) ---------------


def _record_columns(gov, speaker_id: int) -> tuple:
    gov.db.expire_all()
    row = gov.db.get(Speaker, speaker_id)
    return (
        row.identity_source_at,
        row.identity_source_user_id,
        row.identity_source_detail,
        row.suggestion_detail,
    )


def test_a_purge_clears_the_record_of_the_identity_it_reverts(
    client, gov, opensearch, broker, in_process_task, reindexed, events
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Alice")
    machine = make_speaker_row(gov, gov.alice, gov.org, "SPEAKER_00")
    auto_accept_through_the_matcher(gov, gov.alice, machine, profile)
    typed = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_01",
        display_name="Zed",
        verified=True,
        identity_source="human",
        identity_source_user_id=gov.alice.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
    )
    before = _record_columns(gov, machine.id)
    assert before[0] is not None and before[2]["matched_profile_uuid"] == str(profile.uuid), (
        "control: the matcher recorded what it matched"
    )
    assert before[3]["origin"] == "profile_match", "control: and the suggestion's origin"
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    assert _record_columns(gov, machine.id) == (None, None, None, None)
    assert _record_columns(gov, typed.id)[:3] == (
        datetime(2026, 1, 2, tzinfo=UTC),
        gov.alice.id,
        {"origin": "typed"},
    ), "a person's own record is never purged"
    postgres = result["counts"]["postgres"]
    assert postgres["identity_provenance_cleared"] == 1
    assert postgres["suggestion_details_cleared"] == 1
    finished = [e["details"] for e in events if e["details"].get("phase") == "pass_1_completed"]
    assert finished and all(d["residual_ok"] is True for d in finished), finished
    assert all(d["residual"].get("speaker_identity_provenance", 0) == 0 for d in finished)


def test_a_purge_keeps_an_llm_suggestions_detail_beside_a_removed_link(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    profile = make_profile_row(gov, gov.alice, gov.org, "Zed")
    row = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        display_name="Zed",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint_link",
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "profile_propagation"},
        suggested_name="Zed",
        suggestion_source="llm_analysis",
        confidence=0.8,
        suggestion_detail={"origin": "llm", "provider": "vllm", "model": "m"},
    )
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    at, user_id, detail, suggestion_detail = _record_columns(gov, row.id)
    assert (at, user_id, detail) == (None, None, None), "the link's record goes with the link"
    assert suggestion_detail == {"origin": "llm", "provider": "vllm", "model": "m"}
    fresh = gov.db.get(Speaker, row.id)
    assert (fresh.display_name, fresh.profile_id, fresh.suggestion_source) == (
        "Zed",
        None,
        "llm_analysis",
    )
    assert result["counts"]["postgres"]["identity_provenance_cleared"] == 1
    assert result["counts"]["postgres"]["suggestion_details_cleared"] == 0


def test_a_purge_clears_the_detail_of_an_unverified_voice_suggestion(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    row = make_speaker_row(
        gov,
        gov.alice,
        gov.org,
        "SPEAKER_00",
        suggested_name="Ann",
        suggestion_source="voice_match",
        confidence=0.8,
        suggestion_detail={"origin": "voice_match", "matched_speaker_uuid": str(uuid.uuid4())},
    )
    turn_off(gov.db, gov.org, gender=False)

    _task_id, result = run_org_purge(client, gov, broker, in_process_task)

    assert _record_columns(gov, row.id) == (None, None, None, None)
    assert gov.db.get(Speaker, row.id).suggested_name is None
    assert result["counts"]["postgres"]["suggestion_details_cleared"] == 1
    assert result["counts"]["postgres"]["identity_provenance_cleared"] == 0
