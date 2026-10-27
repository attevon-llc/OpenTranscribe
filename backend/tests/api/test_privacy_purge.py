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

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import Any

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

    for label in ("speaker", "profile", "cluster"):
        os_.add(f"{label}-{tag}", owner.id, org_id)
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
        doc_ids={f"{label}-{tag}" for label in ("speaker", "profile", "cluster")},
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
    assert confirmed.suggestion_source == "voice_match", "a verified speaker's suggestion stays"
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
    assert result["counts"]["voiceprint_docs_deleted"] == 6


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
    assert not broker and len(opensearch.docs) == 3


def test_a_member_and_an_anonymous_caller_cannot_purge(client, gov, opensearch, broker):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org)
    assert purge_via_api(client, gov, gov.alice, ORG_PURGE).status_code == status.HTTP_403_FORBIDDEN
    assert client.post(ORG_PURGE, json=BODY).status_code == status.HTTP_401_UNAUTHORIZED
    assert not broker and len(opensearch.docs) == 3


# --------------------------------------------------------------------------- #
# The task
# --------------------------------------------------------------------------- #


def test_the_second_pass_catches_a_document_that_raced_the_switch(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
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

    opensearch.add("raced-write", gov.alice.id, gov.org.id)  # written after pass 1 ran
    result = in_process_task(second_dispatch)
    assert result["status"] == "completed"
    assert "raced-write" not in opensearch.ids()
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
    assert len(opensearch.docs) == 3, "the documents the store refused to delete are still there"


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
    # only the file whose speaker lost its suggestion needs its chunk labels rebuilt
    assert [f[1] for f in reindexed] == [str(a.files[0].uuid)], "the chunk index is not refreshed"


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
            assert e["details"]["scope"] == "organization"
    completed = by_type[AuditEventType.PRIVACY_VOICEPRINT_PURGE][1]
    assert completed["details"]["counts"]["voiceprint_docs_deleted"] == 3
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
    # Only the personal workspace's rows were counted: 2 speakers carry inferred gender, 1 match.
    assert result["counts"]["gender_speakers_cleared"] == 2
    assert result["counts"]["matches_deleted"] == 1
    assert result["counts"]["suggestions_cleared"] == 1


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
    # voiceprint suggestion; the personal workspace's identical rows are not in these numbers.
    assert result["counts"]["gender_speakers_cleared"] == 4
    assert result["counts"]["matches_deleted"] == 2
    assert result["counts"]["suggestions_cleared"] == 2


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
    assert pending == {"task_id": task_id, "status": "pending", "counts": None}

    in_process_task(broker[0])
    in_process_task(broker[1])
    with gov.acting_as(gov.admin):
        done = client.get(url).json()
    assert done["status"] == "completed"
    assert done["counts"]["voiceprint_docs_deleted"] == 3

    with gov.acting_as(gov.alice):
        assert client.get(url).status_code == status.HTTP_404_NOT_FOUND
    with gov.acting_as(gov.admin):
        assert client.get("/api/privacy/purge/not-a-task").status_code == status.HTTP_404_NOT_FOUND
    assert client.get(url).status_code == status.HTTP_401_UNAUTHORIZED
