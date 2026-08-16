"""Functional tests for the search **ops** routes (``endpoints/search.py``).

Six routes that ``scripts/audit-route-coverage.py`` listed as referenced by no
test at all: ``GET /models``, ``POST /models``, ``POST /reindex``,
``POST /reindex/stop``, ``GET /reindex/status`` and ``GET /index-health``. They
are the admin Search panel's entire back end, and three of them are the buttons
an operator presses when search looks broken.

**Nothing here starts a real reindex, and nothing writes a real setting.** The two
side-effecting seams — ``save_search_embedding_model`` (a deployment-wide
``SystemSettings`` write that its own session commits outside the test savepoint)
and ``reindex_transcripts_task`` (a full re-embed of every transcript) — are
replaced with small recording stand-ins, so what is asserted is the *dispatch
contract*: which user the work is attributed to, which files it names, and which
dimension is stored for a given model. A real run against the dev stack would
re-embed the live corpus.

OpenSearch is substituted too, in the two ``index-health`` tests that need a
determinate answer: the handler's job is to report per-index status keyed by the
**alias** the rest of the app uses, and asserting that requires knowing what the
cluster said. The unsubstituted contract test alongside them runs against
whatever cluster is configured, including none.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import status

from app.core.config import settings
from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4

MODELS = "/api/search/models"
REINDEX = "/api/search/reindex"
REINDEX_STOP = "/api/search/reindex/stop"
REINDEX_STATUS = "/api/search/reindex/status"
INDEX_HEALTH = "/api/search/index-health"

#: The four indices ``get_index_health`` probes, in the order it lists them.
EXPECTED_INDICES = (
    get_speaker_index(),
    settings.OPENSEARCH_TRANSCRIPT_INDEX,
    get_speaker_index_v4(),
    settings.OPENSEARCH_CHUNKS_INDEX,
)


class _RecordingTask:
    """Stand-in for a Celery task that records dispatches instead of queueing them.

    A real object rather than a ``Mock`` on purpose: the assertions below are
    ordinary equality checks against ``dispatches``, so they read as behaviour and
    the ``mock-only`` detector has nothing to flag.
    """

    def __init__(
        self, task_id: str = "stand-in-reindex-id", order: list[str] | None = None
    ) -> None:
        self.task_id = task_id
        self.dispatches: list[dict] = []
        #: Shared with ``_RecordingSetter`` when a test needs the ORDER of the two.
        self.order = order if order is not None else []

    def delay(self, **kwargs) -> SimpleNamespace:
        self.dispatches.append(kwargs)
        self.order.append("dispatch")
        return SimpleNamespace(id=self.task_id)


class _UnreachableBrokerTask:
    """A task whose ``.delay()`` fails the way a dead broker really fails.

    Measured against a Celery app pointed at a closed port: ``.delay()`` raises a
    plain ``builtins.RuntimeError`` — "Retry limit exceeded while trying to
    reconnect to the Celery result store backend" — with an empty ``__cause__``
    chain. Not ``kombu.exceptions.OperationalError``, not an ``OSError``. The
    stand-in reproduces that type on purpose: a test that raised a bespoke
    exception would pass against a handler narrowed to the wrong one.
    """

    def __init__(self) -> None:
        self.attempts: list[dict] = []

    def delay(self, **kwargs):
        self.attempts.append(kwargs)
        raise RuntimeError(
            "Retry limit exceeded while trying to reconnect to the Celery result "
            "store backend. The Celery application must be restarted."
        )


class _RecordingSetter:
    """Stand-in for ``save_search_embedding_model``: records, never persists."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.saved: list[tuple[str, int]] = []
        self.order = order if order is not None else []

    def __call__(self, model_id: str, dimension: int) -> None:
        self.saved.append((model_id, dimension))
        self.order.append("save_dimension")


class _StandInRedis:
    """The three operations the reindex lock/cancel flags use, backed by a dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value


class _StandInCat:
    def __init__(self, aliases: dict[str, str], docs: dict[str, int]) -> None:
        self._aliases = aliases
        self._docs = docs

    def aliases(self, **_kwargs) -> list[dict[str, str]]:
        return [{"alias": a, "index": i} for a, i in self._aliases.items()]

    def indices(self, *, index: str, **_kwargs) -> list[dict[str, str]]:
        wanted = set(index.split(","))
        return [{"index": n, "docs.count": str(c)} for n, c in self._docs.items() if n in wanted]


@pytest.fixture
def reindex_task():
    """Replace the reindex task at its import site with a recorder."""
    recorder = _RecordingTask()
    with patch("app.tasks.reindex_task.reindex_transcripts_task", recorder):
        yield recorder


class _StandInMLService:
    """The four ``ml_model_service`` calls a model switch makes, recording each.

    A real recorder rather than a ``Mock`` so the assertions read as "the pipeline
    was pointed at this model", which is the behaviour #437 is about.
    """

    def __init__(self) -> None:
        self.activated: list[str] = []

    def find_model_by_name(self, model_name: str) -> str:
        return f"ml-id-for-{model_name}"

    def get_model_status(self, model_id: str) -> dict:
        return {"model_id": model_id, "deployed": True}

    def set_active_model_id(self, model_id: str) -> None:
        self.activated.append(model_id)


@pytest.fixture
def deployed_model():
    """Every model in the registry answers as registered and deployed.

    Also records the two side effects a switch must have beyond the settings row:
    which model the ingest pipeline was pointed at, and which dimension the index
    was reconciled to. Both are stand-ins — the real calls talk to OpenSearch and
    ``recreate_index_for_dimension`` **deletes the chunks index**.
    """
    service = _StandInMLService()
    recorder = SimpleNamespace(
        activated=service.activated,
        pipelines=[],
        dimensions=[],
    )
    with (
        patch(
            "app.services.search.ml_model_service.get_ml_model_service",
            return_value=service,
        ),
        patch(
            "app.services.search.indexing_service.ensure_neural_ingest_pipeline",
            side_effect=lambda model_id: recorder.pipelines.append(model_id) or True,
        ),
        patch("app.services.search.indexing_service.reset_neural_pipeline_state"),
        patch(
            "app.services.search.indexing_service.recreate_index_for_dimension",
            side_effect=lambda dim: recorder.dimensions.append(dim) or True,
        ),
        patch("app.services.search.hybrid_search_service.clear_search_cache"),
        patch("app.services.search.hybrid_search_service.reset_neural_search_state"),
    ):
        yield recorder


@pytest.fixture
def other_users_files(db_session):
    """A second account owning a COMPLETED file — the user a scoped reindex skips.

    ``_dispatch_reindex_for_every_owner`` opens its **own** ``session_scope``,
    which under the savepoint harness cannot see uncommitted fixture rows, so the
    scope is pointed at ``db_session``. That is what makes the row observable at
    all; it stands in for no behaviour under test.
    """
    import uuid as _uuid

    from app.core.security import get_password_hash
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.user import User

    suffix = str(_uuid.uuid4())[:8]
    owner = User(
        email=f"otherowner_{suffix}@example.com",
        full_name="Other Owner",
        hashed_password=get_password_hash("otherpass"),
        is_active=True,
        role="user",
    )
    db_session.add(owner)
    db_session.flush()

    media = MediaFile(
        uuid=str(_uuid.uuid4()),
        user_id=owner.id,
        filename=f"other_{suffix}.wav",
        storage_path=f"other/{suffix}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=FileStatus.COMPLETED,
    )
    db_session.add(media)
    db_session.flush()

    @contextmanager
    def _savepoint_scope():
        yield db_session

    with patch("app.db.session_utils.session_scope", _savepoint_scope):
        yield SimpleNamespace(owner_id=owner.id, file_uuid=media.uuid)


@pytest.fixture
def standin_redis():
    """Point both reindex-flag readers at one in-memory store.

    Two patch targets because the handler and its helper resolve ``get_redis``
    differently: ``stop_reindex`` uses the name imported into ``search.py``,
    while ``_check_reindex_task_active`` imports it inside the function body.
    """
    fake = _StandInRedis()
    with (
        patch("app.api.endpoints.search.get_redis", return_value=fake),
        patch("app.core.redis.get_redis", return_value=fake),
    ):
        yield fake


# ---------------------------------------------------------------------------
# GET /models — the model picker
# ---------------------------------------------------------------------------
def test_models_lists_the_registry_and_names_the_current_selection(client, user_token_headers):
    """Any active user may read the picker; the current id must be one of the options.

    Catches ``current_model_id`` drifting to a short name or an unregistered value —
    the panel would render a select with nothing chosen and no error.
    """
    response = client.get(MODELS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    offered = {m["model_id"] for m in body["models"]}
    assert offered == set(OPENSEARCH_EMBEDDING_MODELS)
    assert body["current_model_id"] in offered
    for entry in body["models"]:
        assert set(entry) == {"model_id", "name", "dimension", "description", "size_mb"}


def test_models_requires_authentication(client):
    assert client.get(MODELS).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# POST /models — admin only, and it must store the model's own dimension
# ---------------------------------------------------------------------------
def test_setting_a_model_stores_its_dimension_and_reports_the_reindex(
    client, admin_token_headers, admin_user, reindex_task, deployed_model
):
    """The dimension is derived from the registry, never from the request.

    A wrong dimension writes vectors the index cannot hold, so the whole corpus
    silently stops being searchable. The persistence and dispatch seams are
    stand-ins here (see the module docstring) — a real call rewrites a
    deployment-wide setting and re-embeds every transcript.
    """
    model_id, info = next(iter(OPENSEARCH_EMBEDDING_MODELS.items()))
    setter = _RecordingSetter()

    with patch("app.services.search.settings_service.save_search_embedding_model", setter):
        response = client.post(MODELS, headers=admin_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "model_changed"
    assert body["model_id"] == model_id
    assert setter.saved == [(model_id, info["dimension"])]
    assert str(admin_user.id) in body["reindex_task_ids"]


def test_the_dimension_is_persisted_before_any_reindex_is_dispatched(
    client, admin_token_headers, reindex_task, deployed_model
):
    """Ordering is what makes fanning the reindex out over every user safe (#437).

    Each coordinator reconciles the index against
    ``get_search_embedding_dimension()``, and ``recreate_index_for_dimension``
    **deletes the index** when that disagrees with the mapping. With the setting
    already written every coordinator sees a match and no-ops; with a stale one,
    N coordinators race to delete the index out from under each other's workers.
    ``PUT /models/neural/active`` never wrote it at all, so a dimension-changing
    switch recreated the index a second time at the *old* size.
    """
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))
    order: list[str] = []
    setter = _RecordingSetter(order=order)
    dispatcher = _RecordingTask(order=order)

    with (
        patch("app.services.search.settings_service.save_search_embedding_model", setter),
        patch("app.tasks.reindex_task.reindex_transcripts_task", dispatcher),
    ):
        response = client.post(MODELS, headers=admin_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_200_OK
    assert order, "neither the setting nor the dispatch was reached"
    assert order[0] == "save_dimension"
    assert "dispatch" in order


def test_switching_the_model_repoints_the_ingest_pipeline_not_only_the_setting(
    client, admin_token_headers, reindex_task, deployed_model
):
    """Writing the setting alone changes nothing about the vectors (issue #437).

    ``POST /search/models`` used to save ``search.embedding_model`` and dispatch a
    reindex while never touching the ML active model or the ingest pipeline — so
    the reindex re-embedded everything with the **previous** model and the setting
    simply lied. Worse, the coordinator reads the new dimension from that setting
    and ``recreate_index_for_dimension`` *deletes* the index when it disagrees
    with the mapping, so the lie was destructive rather than inert.
    """
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))

    with patch(
        "app.services.search.settings_service.save_search_embedding_model", _RecordingSetter()
    ):
        response = client.post(MODELS, headers=admin_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_200_OK
    assert deployed_model.activated == ["ml-id-for-" + model_id]
    assert deployed_model.pipelines == ["ml-id-for-" + model_id]
    assert deployed_model.dimensions == [OPENSEARCH_EMBEDDING_MODELS[model_id]["dimension"]]


def test_switching_the_model_reindexes_every_owner_not_only_the_caller(
    client, admin_token_headers, admin_user, reindex_task, deployed_model, other_users_files
):
    """A per-caller reindex leaves every other user in the OLD vector space (#437).

    ``reindex_transcripts_task`` filters ``MediaFile.user_id == user_id``, so the
    "full reindex" the endpoint advertised covered only the admin who pressed the
    button. On a multi-user deployment that is not a race — it is what the
    documented happy path does, permanently and silently, and the resulting index
    ranks two models' vectors against each other.
    """
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))

    with patch(
        "app.services.search.settings_service.save_search_embedding_model", _RecordingSetter()
    ):
        response = client.post(MODELS, headers=admin_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_200_OK
    dispatched_for = {d["user_id"] for d in reindex_task.dispatches}
    assert other_users_files.owner_id in dispatched_for
    assert admin_user.id in dispatched_for
    assert all(d["file_uuids"] is None for d in reindex_task.dispatches)
    assert response.json()["reindex_users"] == len(dispatched_for)


def test_a_switch_whose_reindex_cannot_be_queued_fails_loudly(
    client, admin_token_headers, deployed_model
):
    """A half-applied switch must never answer 200 (issue #437).

    By the time the dispatch runs, the settings, the ingest pipeline and the index
    mapping have all changed. If the re-embed cannot be queued, new documents get
    the new model and every existing document keeps the old one — the exact mixed
    vector space this issue exists to prevent. Catching the dispatch failure would
    report that as success with "Re-indexing the transcripts of 0 user(s)": a
    swallowed signal indistinguishable from a working switch.

    There is deliberately no per-user catch either. Only ``user_id`` differs
    between the calls, so no failure can be partial, and a per-call catch would
    buy nothing while converting the global failure into a 200.
    """
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))
    dead_broker = _UnreachableBrokerTask()

    with (
        patch(
            "app.services.search.settings_service.save_search_embedding_model", _RecordingSetter()
        ),
        patch("app.tasks.reindex_task.reindex_transcripts_task", dead_broker),
    ):
        response = client.post(MODELS, headers=admin_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert dead_broker.attempts, "the dispatch was never attempted"
    detail = response.json()["detail"]
    # The operator has to learn that the switch WAS applied — otherwise they retry
    # against a deployment they believe is untouched.
    assert "was switched" in detail
    assert "mixed vector space" in detail


def test_switching_to_an_undeployed_model_is_refused_and_dispatches_nothing(
    client, admin_token_headers, reindex_task
):
    """No deployed model means nothing can embed, so the switch must not be recorded.

    Recording it anyway is what made the legacy path destructive: the coordinator
    would honour the new dimension, delete the whole chunks index, and then fail
    every write because the untouched pipeline still emits the old model's width.
    """
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))
    setter = _RecordingSetter()

    with patch("app.services.search.settings_service.save_search_embedding_model", setter):
        response = client.post(MODELS, headers=admin_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_409_CONFLICT
    assert setter.saved == []
    assert reindex_task.dispatches == []


def test_setting_an_unknown_model_is_400_and_dispatches_nothing(
    client, admin_token_headers, reindex_task
):
    """Validation happens before the write, so a typo cannot trigger a reindex."""
    response = client.post(
        MODELS, headers=admin_token_headers, json={"model_id": "acme/not-a-model"}
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert reindex_task.dispatches == []


def test_setting_a_model_is_refused_for_a_plain_user(client, user_token_headers, reindex_task):
    """A full reindex is deployment-wide work; ``get_current_admin_user`` owns it."""
    model_id = next(iter(OPENSEARCH_EMBEDDING_MODELS))

    response = client.post(MODELS, headers=user_token_headers, json={"model_id": model_id})

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert reindex_task.dispatches == []


def test_setting_a_model_requires_authentication(client, reindex_task):
    response = client.post(MODELS, json={"model_id": next(iter(OPENSEARCH_EMBEDDING_MODELS))})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert reindex_task.dispatches == []


# ---------------------------------------------------------------------------
# POST /reindex — dispatch contract and the pending-only guard
# ---------------------------------------------------------------------------
def test_reindex_dispatches_the_named_files_for_the_calling_admin(
    client, admin_token_headers, admin_user, reindex_task
):
    """``user_id`` comes from the credential and ``file_uuids`` straight from the body.

    Attributing a reindex to the wrong account is the #431 shape: progress goes to
    someone else and the requester waits forever. The task itself is a stand-in —
    a real dispatch re-embeds transcripts on the dev stack.
    """
    response = client.post(
        REINDEX, headers=admin_token_headers, json=["11111111-1111-4111-8111-111111111111"]
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == reindex_task.task_id
    assert reindex_task.dispatches == [
        {"user_id": admin_user.id, "file_uuids": ["11111111-1111-4111-8111-111111111111"]}
    ]


def test_pending_only_reindex_with_nothing_to_index_dispatches_nothing(
    client, admin_token_headers, reindex_task
):
    """The guard: no indexable files means ``no_pending`` and no task at all.

    A freshly created admin owns no completed files, so this exercises the
    early-return branch rather than the sweep. Catches the guard being dropped,
    which would queue an empty full reindex on every panel visit.
    """
    response = client.post(
        REINDEX, headers=admin_token_headers, params={"pending_only": True}, json=None
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "no_pending"
    assert body["task_id"] is None
    assert reindex_task.dispatches == []


def test_reindex_is_refused_for_a_plain_user(client, user_token_headers, reindex_task):
    response = client.post(REINDEX, headers=user_token_headers, json=None)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert reindex_task.dispatches == []


def test_reindex_requires_authentication(client, reindex_task):
    assert client.post(REINDEX, json=None).status_code == status.HTTP_401_UNAUTHORIZED
    assert reindex_task.dispatches == []


# ---------------------------------------------------------------------------
# POST /reindex/stop — the flag, and the "nothing running" answer
# ---------------------------------------------------------------------------
def test_stop_reports_not_running_when_no_lock_is_held(
    client, admin_token_headers, admin_user, standin_redis
):
    """No lock, no cancel flag — a stop request must not create one out of thin air.

    A stray ``reindex_cancel`` key would abort the *next* legitimate reindex after
    its first file.
    """
    response = client.post(REINDEX_STOP, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "not_running"
    assert f"reindex_cancel:{admin_user.id}" not in standin_redis.store


def test_stop_sets_the_cancel_flag_for_the_calling_admin(
    client, admin_token_headers, admin_user, standin_redis
):
    """With the lock held the flag is written, keyed by the caller's own id."""
    standin_redis.store[f"reindex_lock:{admin_user.id}"] = "1"

    response = client.post(REINDEX_STOP, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "stop_requested"
    assert standin_redis.store[f"reindex_cancel:{admin_user.id}"] == "1"


def test_stop_is_refused_for_a_plain_user(client, user_token_headers, standin_redis):
    response = client.post(REINDEX_STOP, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert standin_redis.store == {}


def test_stop_requires_authentication(client, standin_redis):
    assert client.post(REINDEX_STOP).status_code == status.HTTP_401_UNAUTHORIZED
    assert standin_redis.store == {}


# ---------------------------------------------------------------------------
# GET /reindex/status — a user-level read, distinct from the admin verbs
# ---------------------------------------------------------------------------
def test_reindex_status_reports_an_idle_empty_account(client, user_token_headers, standin_redis):
    """A user with no indexable files: zero counts, not in progress, model named.

    Note the privilege asymmetry this pins — *reading* status is
    ``get_current_active_user`` while stopping and starting are admin-only, so a
    plain user seeing 200 here is correct and must not be "tightened".
    """
    response = client.get(REINDEX_STATUS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_files"] == 0
    assert body["pending_files"] == 0
    assert body["in_progress"] is False
    assert body["stop_requested"] is False
    assert body["current_model"] in OPENSEARCH_EMBEDDING_MODELS
    assert isinstance(body["current_dimension"], int)


def test_reindex_status_reflects_a_held_lock_and_a_pending_stop(
    client, user_token_headers, normal_user, standin_redis
):
    """``in_progress`` and ``stop_requested`` are derived from the flags, not constants.

    Hardcoding either one makes the panel offer "Start" during a live reindex, or
    show a stop request that was never made.
    """
    standin_redis.store[f"reindex_lock:{normal_user.id}"] = "1"
    standin_redis.store[f"reindex_cancel:{normal_user.id}"] = "1"

    response = client.get(REINDEX_STATUS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["in_progress"] is True
    assert body["stop_requested"] is True


def test_reindex_status_requires_authentication(client):
    assert client.get(REINDEX_STATUS).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /index-health
# ---------------------------------------------------------------------------
def test_index_health_answers_for_every_probed_index(client, user_token_headers):
    """The contract, against whatever cluster is configured (including none).

    Every probed index must appear with the same three keys and an integer count —
    the panel indexes into all four unconditionally, so a missing key is a render
    error rather than a red badge.
    """
    response = client.get(INDEX_HEALTH, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == set(EXPECTED_INDICES)
    for entry in body.values():
        assert set(entry) == {"status", "doc_count", "error"}
        assert isinstance(entry["doc_count"], int)


def test_index_health_is_red_everywhere_without_a_client(client, user_token_headers):
    """No OpenSearch client is reported as red with a reason, never as green.

    Failing open here is the dangerous direction: the operator would see four green
    badges on a deployment whose search is entirely down.
    """
    with patch("app.services.opensearch_service.opensearch_client", None):
        response = client.get(INDEX_HEALTH, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [e["status"] for e in body.values()] == ["red"] * len(EXPECTED_INDICES)
    assert [e["error"] for e in body.values()] == ["OpenSearch client not initialized"] * len(
        EXPECTED_INDICES
    )


def test_index_health_keys_an_aliased_index_by_its_alias(client, user_token_headers):
    """Doc counts arrive keyed by the concrete index but must be reported per alias.

    The speaker indices are aliases; keying the response by the concrete name would
    leave the panel looking up a key that is not there and reporting "missing" for a
    perfectly healthy index. Substituting the cluster is the only way to know what it
    said — hence the stand-in.
    """
    alias = EXPECTED_INDICES[0]
    concrete = f"{alias}-000001"
    standin = SimpleNamespace(
        cat=_StandInCat(
            aliases={alias: concrete},
            docs={concrete: 7, **{name: 0 for name in EXPECTED_INDICES[1:]}},
        )
    )

    with patch("app.services.opensearch_service.opensearch_client", standin):
        response = client.get(INDEX_HEALTH, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body[alias] == {"status": "green", "doc_count": 7, "error": None}
    assert body[EXPECTED_INDICES[1]]["status"] == "green"


def test_index_health_requires_authentication(client):
    assert client.get(INDEX_HEALTH).status_code == status.HTTP_401_UNAUTHORIZED
