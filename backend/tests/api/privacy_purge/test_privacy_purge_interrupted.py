"""A purge whose worker dies after pass 1's relational commit (issue #1334 item 2).

Pass 1 deletes the cluster ROWS in its relational commit and their ``cluster_<uuid>`` documents
afterwards. Before v461 the ids of documents it could not confirm gone travelled only in the pass-2
Celery message, so a worker killed in between took them with it: the centroid document survived,
nothing ever retried it, and a later purge of the same scope measured a clean residual and reported
``completed``.

Each test kills the worker (a ``BaseException``, which no ``except Exception`` handler in the task
catches, as with SIGKILL) at one point after the commit, lets the scheduled privacy recovery run,
then asks for the same purge again. The purge must finish the centroid work or say it is incomplete;
it must never report done while the document survives.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.celery import celery_app
from app.core.celery_beat import BEAT_SCHEDULE
from app.models.media import Task
from app.models.speaker import SpeakerCluster
from app.services.privacy import purge
from app.tasks.privacy_purge_task import privacy_purge_task
from tests.fixtures.privacy_purge import ME_PURGE
from tests.fixtures.privacy_purge import REVERIFY_LEASE_KEY
from tests.fixtures.privacy_purge import purge_via_api
from tests.fixtures.privacy_purge import seed
from tests.fixtures.privacy_purge import turn_off

VOICEPRINTS_ONLY = {"targets": ["voiceprints"], "confirm": "PURGE"}

# Every test here runs the re-verify, which takes its lease on a real (throwaway) Redis.
pytestmark = [
    pytest.mark.usefixtures("reverify_lease"),
    pytest.mark.xdist_group("local_model_redis"),
]


class WorkerLost(BaseException):
    """The worker process is gone: no handler in the task body runs."""


@pytest.fixture
def status_record(monkeypatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(
        "app.core.redis.get_request_redis",
        lambda: SimpleNamespace(
            set=lambda key, value, ex=None: store.__setitem__(key, value),
            get=store.get,
            zrem=lambda *_args: 0,
        ),
    )
    return store


@pytest.fixture
def dying_broker(monkeypatch) -> dict[str, Any]:
    """Records every dispatch; ``die_on_dispatch`` kills the worker at the pass-2 dispatch."""
    state: dict[str, Any] = {"sent": [], "die_on_dispatch": False}

    def dispatch(args=None, kwargs=None, **options):
        if state["die_on_dispatch"] and (kwargs or {}).get("phase") == 2:
            raise WorkerLost
        state["sent"].append({"kwargs": dict(kwargs or {}), **options})

    monkeypatch.setattr(privacy_purge_task, "apply_async", dispatch)
    return state


def _run(sent: dict[str, Any]) -> dict[str, Any]:
    outcome: dict[str, Any] = privacy_purge_task.apply(
        kwargs=sent["kwargs"], task_id=sent["task_id"]
    ).get(propagate=True)
    return outcome


def _run_scheduled_privacy_recovery() -> int:
    """Run every scheduled ``privacy.purge*`` task once, with its waiting period already over."""
    names = sorted(
        {
            entry["task"]
            for entry in BEAT_SCHEDULE.values()
            if str(entry["task"]).startswith("privacy.purge")
        }
    )
    for name in names:
        celery_app.tasks[name].apply(kwargs={"grace_seconds": 0}).get(propagate=True)
    return len(names)


def _status(client, gov, task_id: str) -> dict[str, Any]:
    with gov.acting_as(gov.alice):
        shown: dict[str, Any] = client.get(f"/api/privacy/purge/{task_id}").json()
    return shown


@pytest.mark.usefixtures("in_process_task", "reindexed", "status_record")
@pytest.mark.parametrize("dies", ["before_the_document_delete", "at_the_pass_two_dispatch"])
def test_a_purge_whose_worker_dies_after_the_commit_never_reports_done_with_a_centroid_left(
    client, gov, opensearch, dying_broker, events, monkeypatch, dies
):
    world = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    turn_off(gov.db, gov.org, gender=False)
    document = f"cluster_{gov.db.get(SpeakerCluster, world.loose).uuid}"
    assert document in opensearch.ids()

    real_docs = purge.purge_voiceprint_docs
    real_delete = opensearch.delete_by_query
    if dies == "before_the_document_delete":

        def killed(*_args, **_kwargs):
            raise WorkerLost

        monkeypatch.setattr(purge, "purge_voiceprint_docs", killed)
    else:
        # The centroid delete fails in pass 1, and the worker dies dispatching pass 2.
        def flaky(index, body, refresh=True, conflicts="proceed"):
            if document in body.get("query", {}).get("ids", {}).get("values", []):
                raise ConnectionError("transient index failure")
            return real_delete(index, body, refresh, conflicts)

        monkeypatch.setattr(opensearch, "delete_by_query", flaky)
        dying_broker["die_on_dispatch"] = True

    response = purge_via_api(client, gov, gov.alice, ME_PURGE, VOICEPRINTS_ONLY)
    first_id = response.json()["task_id"]
    with pytest.raises(WorkerLost):
        _run(dying_broker["sent"][-1])
    gov.db.expire_all()
    assert gov.db.get(SpeakerCluster, world.loose) is None, "pass 1's relational commit landed"
    assert document in opensearch.ids()

    # The worker is gone; the stores are healthy again.
    monkeypatch.setattr(purge, "purge_voiceprint_docs", real_docs)
    monkeypatch.setattr(opensearch, "delete_by_query", real_delete)
    dying_broker["die_on_dispatch"] = False
    _run_scheduled_privacy_recovery()

    # The person asks again, as a failed or stuck purge invites them to.
    again = purge_via_api(client, gov, gov.alice, ME_PURGE, VOICEPRINTS_ONLY).json()["task_id"]
    _run(dying_broker["sent"][-1])
    final = _run(dying_broker["sent"][-1])
    assert final["status"] == "completed" and _status(client, gov, again)["status"] == "completed"
    assert document not in opensearch.ids(), (
        "the purge reported completed while a centroid document it was asked to delete survives"
    )

    # The lost purge itself reports incomplete, never done and never in progress for ever.
    gov.db.expire_all()
    shown = _status(client, gov, first_id)
    assert shown["status"] == "failed" and shown["reason"] == "interrupted"
    assert gov.db.get(Task, first_id).status == "failed"
    reverified = [
        e
        for e in events
        if e["details"].get("phase") == "reverify" and e["details"]["task_id"] == first_id
    ]
    assert len(reverified) == 1
    assert reverified[0]["outcome"].value == "failure"
    assert reverified[0]["details"]["reason"] == "interrupted"
    assert reverified[0]["details"]["residual"] == {"cluster_docs": 0}
    assert reverified[0]["details"]["purge_scope"]["user_id"] == gov.alice.id


# --- the re-verify on its own ------------------------------------------------------------------


def _pending_purge(gov, opensearch, *, task_status: str, age_seconds: float) -> tuple[str, str]:
    """A purge task row plus its pending row naming one centroid document that still exists."""
    from app.models.privacy_purge_pending import PrivacyPurgePending

    task_id, cluster_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    gov.db.add(
        Task(id=task_id, user_id=gov.alice.id, task_type="privacy_purge", status=task_status)
    )
    gov.db.add(
        PrivacyPurgePending(
            task_id=task_id,
            scope=purge.PurgeScope(
                "user", organization_id=gov.org.id, user_id=gov.alice.id
            ).to_payload(),
            cluster_uuids=[cluster_uuid],
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
    )
    gov.db.commit()
    opensearch.add(f"cluster_{cluster_uuid}", gov.alice.id, None, document_type="cluster")
    return task_id, f"cluster_{cluster_uuid}"


def _row(gov, task_id: str):
    from app.models.privacy_purge_pending import PrivacyPurgePending

    gov.db.expire_all()
    return gov.db.get(PrivacyPurgePending, task_id)


def _reverify(**kwargs) -> dict[str, int]:
    from app.tasks.privacy_purge_task import privacy_purge_reverify_task

    summary: dict[str, int] = privacy_purge_reverify_task.apply(kwargs=kwargs).get(propagate=True)
    return summary


@pytest.mark.usefixtures("in_process_task", "status_record")
def test_a_purge_younger_than_the_grace_period_is_left_to_its_own_second_pass(
    gov, opensearch, events
):
    task_id, document = _pending_purge(gov, opensearch, task_status="in_progress", age_seconds=60)

    assert _reverify()["rows"] == 0

    assert document in opensearch.ids()
    assert _row(gov, task_id) is not None
    assert gov.db.get(Task, task_id).status == "in_progress"
    assert not [e for e in events if e["details"].get("phase") == "reverify"]


@pytest.mark.usefixtures("in_process_task", "status_record")
def test_a_store_still_failing_keeps_the_row_and_counts_the_attempt(
    gov, opensearch, events, monkeypatch
):
    task_id, document = _pending_purge(gov, opensearch, task_status="failed", age_seconds=7200)

    def down(index, body, refresh=True, conflicts="proceed"):
        raise ConnectionError("index unavailable")

    monkeypatch.setattr(opensearch, "delete_by_query", down)
    summary = _reverify(grace_seconds=0)

    assert summary == {"rows": 1, "finished": 0, "remaining": 1, "interrupted": 0, "skipped": 0}
    assert document in opensearch.ids()
    row = _row(gov, task_id)
    assert row is not None and row.reverify_attempts == 1 and row.last_reverify_at is not None
    event = next(e for e in events if e["details"].get("phase") == "reverify")
    assert event["outcome"].value == "failure" and event["details"]["reason"] == "residual"
    assert event["details"]["residual"] == {"cluster_docs": 1}
    # The row is two hours old, but the attempt just made is fresh: not retried until it ages.
    assert _reverify(grace_seconds=60)["rows"] == 0
    assert _reverify(grace_seconds=0)["rows"] == 1
    assert _row(gov, task_id).reverify_attempts == 2


@pytest.mark.usefixtures("in_process_task", "status_record")
def test_a_finished_purge_keeps_its_status_and_its_row_goes_with_the_document(
    gov, opensearch, events
):
    task_id, document = _pending_purge(gov, opensearch, task_status="failed", age_seconds=7200)

    summary = _reverify(grace_seconds=0)

    assert summary == {"rows": 1, "finished": 1, "remaining": 0, "interrupted": 0, "skipped": 0}
    assert document not in opensearch.ids()
    assert _row(gov, task_id) is None
    assert gov.db.get(Task, task_id).status == "failed", "the purge as it ran stays on record"
    event = next(e for e in events if e["details"].get("phase") == "reverify")
    assert event["outcome"].value == "success" and "reason" not in event["details"]
    assert event["details"]["counts"]["opensearch"] == {"cluster_docs_deleted": 1}
    assert event["organization_id"] == gov.org.id


@pytest.mark.usefixtures("in_process_task", "status_record")
def test_a_reverify_that_overlaps_a_running_one_takes_nothing(
    gov, opensearch, events, reverify_lease
):
    """The beat tick and an administrator's run must not both take the same purge.

    Two runs over one row both deleted, both audited and both failed the purge as interrupted.
    The second run now finds the lease held and leaves the row to the run that holds it.
    """
    task_id, document = _pending_purge(gov, opensearch, task_status="in_progress", age_seconds=7200)
    reverify_lease.set(REVERIFY_LEASE_KEY, "another-run", ex=60)

    assert _reverify(grace_seconds=0) == {
        "rows": 0,
        "finished": 0,
        "remaining": 0,
        "interrupted": 0,
        "skipped": 1,
    }
    assert document in opensearch.ids()
    assert _row(gov, task_id) is not None
    assert gov.db.get(Task, task_id).status == "in_progress"
    assert not [e for e in events if e["details"].get("phase") == "reverify"]

    reverify_lease.delete(REVERIFY_LEASE_KEY)
    import app.tasks.privacy_purge_task as task_module

    assert task_module.REVERIFY_LOCK_KEY == REVERIFY_LEASE_KEY, "the test holds the real lease"
    assert _reverify(grace_seconds=0)["interrupted"] == 1
    assert document not in opensearch.ids()
    assert len([e for e in events if e["details"].get("phase") == "reverify"]) == 1
