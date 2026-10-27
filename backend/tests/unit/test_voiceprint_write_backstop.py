"""Tier 3 of the privacy policy: every OpenSearch voiceprint write is refused for a forbidden scope.

A recording client stands in for the cluster, so the assertion is "was a document written", with
the real policy deciding from the rows a test wrote. Each write function is driven for an allowed
scope (the control, which MUST write) and for each layer that can forbid it. The backstop is the
last line: it is what holds when an entry gate is missing, so these tests call the write function
directly, with no gate in front of it.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from unittest.mock import MagicMock

import pytest

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.services.opensearch_service import clusters
from app.services.opensearch_service import profiles
from app.services.opensearch_service import speaker_write

SCOPES = ["allowed", "user", "organization", "instance"]
V4 = [0.01] * PYANNOTE_EMBEDDING_DIMENSION_V4
V3 = [0.01] * PYANNOTE_EMBEDDING_DIMENSION_V3


@pytest.fixture
def cluster(monkeypatch):
    """A recording stand-in for the OpenSearch client every write module shares."""
    from app.services.opensearch_service import client as _client

    fake = MagicMock(name="opensearch")
    fake.get.return_value = {"_source": {"user_id": 0, "label": "x"}}
    monkeypatch.setattr(_client, "opensearch_client", fake)
    for module in (speaker_write, profiles, clusters):
        monkeypatch.setattr(module, "ensure_indices_exist", lambda *a, **k: None)
    monkeypatch.setattr(clusters, "get_active_speaker_index", lambda: "speakers")
    monkeypatch.setattr(clusters, "query_embedding_dimension_mismatch", lambda *a, **k: False)
    return fake


def _scope(world, user, scope: str) -> int | None:
    """Switch `scope` off for `user`; returns the organization id of the documents."""
    if scope == "user":
        world.set_user(user, "voiceprints", "false")
    elif scope == "instance":
        world.set_instance("voiceprints", "false")
    elif scope == "organization":
        org = world.new_org()
        world.set_org(org.id, voiceprints=False)
        return int(org.id)
    return None


def _written(cluster) -> int:
    return int(cluster.index.call_count + cluster.bulk.call_count)


def _speaker_kwargs(user, org_id):
    return {
        "speaker_id": 1,
        "speaker_uuid": str(uuid_pkg.uuid4()),
        "user_id": user.id,
        "name": "SPEAKER_00",
        "organization_id": org_id,
    }


@pytest.mark.parametrize("scope", SCOPES)
def test_add_speaker_embedding_v4_writes_only_when_allowed(
    privacy_world, cluster, normal_user, scope
):
    org_id = _scope(privacy_world, normal_user, scope)

    speaker_write.add_speaker_embedding_v4(embedding=V4, **_speaker_kwargs(normal_user, org_id))

    assert _written(cluster) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_add_speaker_embedding_writes_only_when_allowed(privacy_world, cluster, normal_user, scope):
    org_id = _scope(privacy_world, normal_user, scope)

    speaker_write.add_speaker_embedding(embedding=V3, **_speaker_kwargs(normal_user, org_id))

    assert _written(cluster) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_store_profile_embedding_v4_writes_only_when_allowed(
    privacy_world, cluster, normal_user, scope
):
    org_id = _scope(privacy_world, normal_user, scope)

    stored = profiles.store_profile_embedding_v4(
        profile_id=1,
        profile_uuid=str(uuid_pkg.uuid4()),
        profile_name="Ada",
        embedding=V4,
        speaker_count=2,
        user_id=normal_user.id,
        organization_id=org_id,
    )

    assert stored is (scope == "allowed")
    assert _written(cluster) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_store_profile_embedding_writes_only_when_allowed(
    privacy_world, cluster, normal_user, scope
):
    org_id = _scope(privacy_world, normal_user, scope)

    stored = profiles.store_profile_embedding(
        profile_id=1,
        profile_uuid=str(uuid_pkg.uuid4()),
        profile_name="Ada",
        embedding=V4,
        speaker_count=2,
        user_id=normal_user.id,
        organization_id=org_id,
    )

    assert stored is (scope == "allowed")
    assert _written(cluster) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_store_cluster_embedding_writes_only_when_allowed(
    privacy_world, cluster, normal_user, scope
):
    org_id = _scope(privacy_world, normal_user, scope)

    stored = clusters.store_cluster_embedding(
        str(uuid_pkg.uuid4()), normal_user.id, V4, organization_id=org_id
    )

    assert stored is (scope == "allowed")
    assert _written(cluster) == (1 if scope == "allowed" else 0)


@pytest.mark.parametrize("scope", SCOPES)
def test_update_cluster_embedding_judges_the_scope_the_stored_document_carries(
    privacy_world, cluster, normal_user, scope
):
    org_id = _scope(privacy_world, normal_user, scope)
    source = {"user_id": normal_user.id, "label": "c"}
    if org_id is not None:
        source["organization_id"] = org_id
    cluster.get.return_value = {"_source": source}

    updated = clusters.update_cluster_embedding(str(uuid_pkg.uuid4()), V4)

    assert updated is (scope == "allowed")
    assert _written(cluster) == (1 if scope == "allowed" else 0)


# ------------------------------------------------------------------ bulk: per entry


def _bulk_entry(user, org_id):
    return {
        "speaker_uuid": str(uuid_pkg.uuid4()),
        "speaker_id": 1,
        "user_id": user.id,
        "name": "SPEAKER_00",
        "embedding": V4,
        "organization_id": org_id,
    }


def _bulk_ids(cluster) -> list[str]:
    body = cluster.bulk.call_args.kwargs["body"]
    return [action["index"]["_id"] for action in body[::2]]


def test_a_bulk_write_drops_only_the_forbidden_scopes_entries(
    privacy_world, cluster, normal_user, other_user
):
    privacy_world.set_user(other_user, "voiceprints", "false")
    allowed = _bulk_entry(normal_user, None)
    forbidden = _bulk_entry(other_user, None)

    speaker_write.bulk_add_speaker_embeddings_v4([forbidden, allowed])

    assert _bulk_ids(cluster) == [allowed["speaker_uuid"]]


def test_a_bulk_write_of_only_forbidden_entries_sends_nothing(privacy_world, cluster, normal_user):
    privacy_world.set_user(normal_user, "voiceprints", "false")

    response = speaker_write.bulk_add_speaker_embeddings_v4(
        [_bulk_entry(normal_user, None), _bulk_entry(normal_user, None)]
    )

    assert response is None
    assert cluster.bulk.call_count == 0


def test_a_bulk_write_of_allowed_entries_sends_them_all(privacy_world, cluster, normal_user):
    entries = [_bulk_entry(normal_user, None), _bulk_entry(normal_user, None)]

    speaker_write.bulk_add_speaker_embeddings_v4(entries)

    assert _bulk_ids(cluster) == [e["speaker_uuid"] for e in entries]


# ------------------------------------------------------------------ failure policy


def test_a_write_the_policy_cannot_decide_is_refused(
    privacy_world, cluster, normal_user, monkeypatch
):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr("app.db.session_utils.session_scope", broken)
    privacy_world.reset()

    speaker_write.add_speaker_embedding_v4(embedding=V4, **_speaker_kwargs(normal_user, None))

    assert _written(cluster) == 0


def test_a_switch_off_takes_effect_for_the_next_write_once_the_cache_is_cleared(
    privacy_world, cluster, normal_user
):
    speaker_write.add_speaker_embedding_v4(embedding=V4, **_speaker_kwargs(normal_user, None))
    assert _written(cluster) == 1

    privacy_world.set_user(normal_user, "voiceprints", "false")
    privacy_world.reset()  # the 10 s per-process cache; a deployment waits it out
    speaker_write.add_speaker_embedding_v4(embedding=V4, **_speaker_kwargs(normal_user, None))

    assert _written(cluster) == 1


def test_a_refused_write_is_logged_once_per_scope(privacy_world, cluster, normal_user, caplog):
    privacy_world.set_user(normal_user, "voiceprints", "false")

    with caplog.at_level(logging.WARNING, logger="app.services.privacy.policy"):
        for _ in range(3):
            speaker_write.add_speaker_embedding_v4(
                embedding=V4, **_speaker_kwargs(normal_user, None)
            )

    refusals = [r for r in caplog.records if "refused by policy" in r.getMessage()]
    assert len(refusals) == 1
    assert _written(cluster) == 0
