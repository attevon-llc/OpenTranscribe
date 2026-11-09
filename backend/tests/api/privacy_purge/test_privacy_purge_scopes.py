"""Organization purges over HTTP and through the task body: tenant scoping, confirmation, switch ordering, audit, status.

Fixtures (in-memory OpenSearch, recorded broker, in-process task runner) live in
``tests/api/privacy_purge/conftest.py``; seeding helpers in ``tests/fixtures/privacy_purge.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi import status

from app.auth.audit_events import AuditEventType
from app.core import constants
from app.models.media import Task
from app.models.organization_policy import OrganizationPolicy
from app.models.speaker import Speaker
from app.services.privacy import purge
from app.tasks.privacy_purge_task import privacy_purge_task
from tests.fixtures.privacy_purge import BODY
from tests.fixtures.privacy_purge import ORG_PURGE
from tests.fixtures.privacy_purge import assert_purged
from tests.fixtures.privacy_purge import assert_untouched
from tests.fixtures.privacy_purge import purge_via_api
from tests.fixtures.privacy_purge import seed
from tests.fixtures.privacy_purge import turn_off

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

    monkeypatch.setattr("app.services.privacy.policy._stored_layers_many", broken)
    assert purge.targets_still_enabled(gov.db, scope, ["voiceprints"]) == ["voiceprints"]


def test_a_scope_cannot_be_built_without_what_it_needs():
    with pytest.raises(ValueError):
        purge.PurgeScope("organization")
    with pytest.raises(ValueError):
        purge.PurgeScope("user")
    with pytest.raises(ValueError):
        purge.PurgeScope("instance_all", organization_id=1)
    with pytest.raises(ValueError):
        # Off-contract kind on purpose: the constructor must refuse a scope it does not know.
        purge.PurgeScope(cast(purge.ScopeKind, "everyone"))


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
            assert e["details"]["purge_scope"] == {
                "kind": "organization",
                "organization_id": gov.org.id,
                "user_id": None,
            }
    completed = by_type[AuditEventType.PRIVACY_VOICEPRINT_PURGE][1]
    assert sum(completed["details"]["counts"]["opensearch"].values()) == 6
    assert "Ann" not in repr(events) and gov.alice.email not in repr(events)


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
    monkeypatch.setattr("app.core.redis.get_request_redis", lambda: fake_redis)
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
        "reason": None,
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
