"""Purge verification: residual counts, the audit record, the second centroid pass and the record columns of a reverted identity.

Fixtures (in-memory OpenSearch, recorded broker, in-process task runner) live in
``tests/api/privacy_purge/conftest.py``; seeding helpers in ``tests/fixtures/privacy_purge.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.auth.audit_events import AuditEventType
from app.models.media import Task
from app.models.privacy_purge_pending import PrivacyPurgePending
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.services.privacy import purge
from tests.fixtures.privacy_purge import ME_PURGE
from tests.fixtures.privacy_purge import ORG_PURGE
from tests.fixtures.privacy_purge import auto_accept_through_the_matcher
from tests.fixtures.privacy_purge import make_profile_row
from tests.fixtures.privacy_purge import make_speaker_row
from tests.fixtures.privacy_purge import purge_via_api
from tests.fixtures.privacy_purge import run_org_purge
from tests.fixtures.privacy_purge import seed
from tests.fixtures.privacy_purge import turn_off


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
        "app.core.redis.get_request_redis",
        lambda: SimpleNamespace(
            set=lambda key, value, ex=None: store.__setitem__(key, value),
            get=store.get,
            zrem=lambda *_args: 0,
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
    assert details["purge_scope"] == {
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
# still indexed. The ids therefore live in the purge's ``privacy_purge_pending`` row (v461),
# written in the relational commit, until they are measured gone.
# --------------------------------------------------------------------------- #

VOICEPRINTS_ONLY = {"targets": ["voiceprints"], "confirm": "PURGE"}


@pytest.fixture
def status_record(monkeypatch):
    """The purge's Redis status record, as a dict."""
    store: dict[str, str] = {}
    monkeypatch.setattr(
        "app.core.redis.get_request_redis",
        lambda: SimpleNamespace(
            set=lambda key, value, ex=None: store.__setitem__(key, value),
            get=store.get,
            zrem=lambda *_args: 0,
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


def _pending(gov, task_id: str) -> list[str] | None:
    """The centroid ids the purge's pending row lists, or None once the row is gone."""
    gov.db.expire_all()
    row = gov.db.get(PrivacyPurgePending, task_id)
    return None if row is None else list(row.cluster_uuids)


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
    task_id = _request_purge(client, gov, opensearch, document, scope).json()["task_id"]

    first = in_process_task(broker[0])
    in_pass_one["yes"] = False
    assert document.removeprefix("cluster_") in (_pending(gov, task_id) or [])
    final = in_process_task(broker[1])

    assert first["second_pass"] == "scheduled"
    assert "carried_cluster_uuids" not in broker[1]["kwargs"], "the ids live in Postgres now"
    assert document not in opensearch.ids(), "pass two retried the document pass one left"
    assert _pending(gov, task_id) is None, "nothing is left for the re-verify"
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
    task_id = _request_purge(client, gov, opensearch, document, scope).json()["task_id"]

    in_process_task(broker[0])
    final = in_process_task(broker[1])

    assert document in opensearch.ids()
    assert document.removeprefix("cluster_") in (_pending(gov, task_id) or []), (
        "kept for the re-verify"
    )
    assert final["status"] == "failed" and final["reason"] == "residual"
    assert final["residual"]["cluster_docs"] >= 1, "pass two still measures the carried id"
    finished = [e for e in events if e["details"].get("phase") == "pass_2_completed"]
    assert finished[0]["outcome"].value == "failure"


@pytest.mark.usefixtures("status_record")
def test_a_clean_first_pass_leaves_an_empty_pending_row_the_final_pass_removes(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    task_id = purge_via_api(client, gov, gov.alice, ME_PURGE, VOICEPRINTS_ONLY).json()["task_id"]

    in_process_task(broker[0])
    assert _pending(gov, task_id) == [], "the row stays until the final pass ends"

    in_process_task(broker[1])
    assert _pending(gov, task_id) is None


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
