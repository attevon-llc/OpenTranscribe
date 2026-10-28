"""Deployment-wide maintenance operations need the deployment-scope right.

A reindex, an index repair, an embedding-model switch, an orphan sweep: each acts on every
tenant's data at once, not on the caller's tenant. ``PlatformBypass.deployment_scope_writable``
is the one rule for that reach: any admin in a single-tenant deployment, a ``super_admin`` in a
multi-tenant one. These tests pin it for every such route, from three sides:

* multi-tenant, plain admin: refused with 403, and the operation's first real step never runs;
* multi-tenant, ``super_admin``: served;
* single-tenant, plain admin: served (unchanged).

"Served" is observed through a sentinel, not by running the operation. The first side-effecting
call of each handler is replaced with one that answers ``418``; a ``418`` therefore means the
gate let the request through and the handler got as far as doing work, and the recording of
whether that call happened is what distinguishes a refusal from a pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
from app.models.media import SpeakerProfile
from app.services.platform_access import reset_tenancy_mode_cache

SENTINEL = 418
MODEL = next(iter(OPENSEARCH_EMBEDDING_MODELS))


@dataclass(frozen=True)
class Op:
    """One deployment-wide route, and the call that proves its handler started working."""

    method: str
    path: str
    first_step: str
    extra_patches: dict[str, Any] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)
    needs_profile: bool = False

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


OPS = (
    Op(
        "POST",
        "/api/search/reindex",
        "app.services.search.model_switch.dispatch_reindex_for_every_owner",
    ),
    Op("POST", "/api/search/reindex/stop", "app.api.endpoints.search.read_fanout"),
    Op(
        "POST",
        "/api/search/repair-indices",
        "app.api.endpoints.search._probe_index_health",
        extra_patches={"app.services.opensearch_service.opensearch_client": object()},
    ),
    Op(
        "POST",
        "/api/search/models",
        "app.api.endpoints.search._switch_model",
        kwargs={"json": {"model_id": MODEL}},
    ),
    Op(
        "POST",
        f"/api/search/models/neural/{MODEL}/register",
        "app.services.search.ml_model_service.get_ml_model_service",
    ),
    Op(
        "POST",
        f"/api/search/models/neural/{MODEL}/deploy",
        "app.services.search.ml_model_service.get_ml_model_service",
    ),
    Op(
        "POST",
        f"/api/search/models/neural/{MODEL}/undeploy",
        "app.services.search.ml_model_service.get_ml_model_service",
    ),
    Op(
        "PUT",
        "/api/search/models/neural/active",
        "app.api.endpoints.search._switch_model",
        kwargs={"params": {"model_name": MODEL}},
    ),
    Op(
        "POST",
        "/api/admin/data-integrity",
        "app.tasks.opensearch_integrity_task.get_integrity_status",
    ),
    Op(
        "POST",
        "/api/admin/data-integrity/storage",
        "app.tasks.storage_orphan_sweep_task.get_storage_sweep_status",
        kwargs={"params": {"confirm": "true"}},
    ),
    Op(
        "POST",
        "/api/admin/embedding-consistency/repair",
        "app.tasks.speaker_embedding_consistency.get_embedding_consistency_status",
    ),
    Op(
        "POST",
        "/api/admin/embedding-consistency/stop",
        "app.tasks.speaker_embedding_consistency.stop_consistency_repair",
    ),
    Op(
        "POST",
        "/api/admin/profile-embeddings/repair",
        "app.services.profile_embedding_service.ProfileEmbeddingService.batch_update_profile_embeddings",
        needs_profile=True,
    ),
    Op(
        "POST",
        "/api/admin/imohash-recompute/start",
        "app.tasks.imohash_recompute.recompute_progress.is_running",
    ),
    Op(
        "POST",
        "/api/admin/settings/cache-config/clear",
        "app.services.cache_management_service.clear_derived_cache",
    ),
    Op("POST", "/api/admin/settings/retention-config/run", "app.core.celery.celery_app.send_task"),
)


@contextmanager
def _sentinel(op: Op) -> Iterator[MagicMock]:
    with ExitStack() as stack:
        for target, replacement in op.extra_patches.items():
            stack.enter_context(patch(target, new=replacement))
        yield stack.enter_context(patch(op.first_step, side_effect=HTTPException(SENTINEL)))


def _send(w, op: Op, headers):
    if op.needs_profile:
        w.db.add(SpeakerProfile(user_id=w.admin.id, name="gate-probe-profile"))
        w.db.commit()
    with _sentinel(op) as first_step:
        response = w.client.request(op.method, op.path, headers=headers, **op.kwargs)
    return response, first_step


def _single_tenant(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()


@pytest.fixture
def single_tenant(monkeypatch) -> Iterator[None]:
    _single_tenant(monkeypatch)
    yield
    reset_tenancy_mode_cache()


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.label)
def test_a_plain_admin_in_a_multi_tenant_deployment_is_refused(support_world, op):
    response, first_step = _send(support_world, op, support_world.admin_headers)

    assert response.status_code == 403, response.text
    assert "super admin" in response.json()["detail"]
    assert not first_step.called, "the operation started for a caller who was refused"


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.label)
def test_a_super_admin_in_a_multi_tenant_deployment_is_served(support_world, op):
    response, first_step = _send(support_world, op, support_world.super_headers)

    assert response.status_code == SENTINEL, response.text
    assert first_step.called


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.label)
def test_an_admin_in_a_single_tenant_deployment_is_served(support_world, single_tenant, op):
    response, first_step = _send(support_world, op, support_world.admin_headers)

    assert response.status_code == SENTINEL, response.text
    assert first_step.called


def test_a_non_admin_is_still_refused_before_the_deployment_scope_question(support_world):
    """The control for the gate order: the role check is unchanged and still comes first."""
    w = support_world
    for op in OPS:
        response, first_step = _send(w, op, w.owner_headers)
        assert response.status_code == 403, op.label
        assert "super admin" not in str(response.json()["detail"]), op.label
        assert not first_step.called, op.label
