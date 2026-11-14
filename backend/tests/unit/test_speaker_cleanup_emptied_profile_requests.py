"""A speaker change that empties a profile erases its embedding in three OpenSearch requests.

The speaker-merge request reconciles the profile the merged-away speaker belonged to
before it answers. When that speaker was the profile's only member, the stored embedding
averages nobody and must be erased. A 100-user load run traced about twenty
sequential OpenSearch requests per such merge: the profile document was deleted once by a
best-effort clear inside ``update_profile_embedding`` and then again by the verified
erasure, which itself checked, swept, re-checked, refreshed and counted each speaker index
in turn. The erase now runs once, as one bulk delete, one refresh and one survivor read.

Runs ``reconcile_speaker_impact`` for real down to the OpenSearch client; only the
Postgres session (a mock answering the profile reads) and the cache invalidation are
stand-ins. Every request the real client would send lands in the stand-in's ``calls``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import opensearch_erasure as ose
from app.services import speaker_cleanup as sc
from tests.fixtures.speaker_family_cluster import SpeakerFamilyCluster

PROFILE_ID = 5
PROFILE_UUID = "p-uuid"


class _ProfileRow(SimpleNamespace):
    """Answers both the ORM read (attributes) and the ``(uuid,)`` column read (index 0)."""

    def __getitem__(self, i: int) -> Any:
        assert i == 0
        return self.uuid


def _db_with_an_emptied_profile() -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _ProfileRow(
        id=PROFILE_ID,
        uuid=PROFILE_UUID,
        name="Load Test",
        user_id=1,
        organization_id=None,
        embedding_count=1,
        last_embedding_update=None,
    )
    db.query.return_value.filter.return_value.all.return_value = []  # no member left
    return db


@pytest.fixture
def cluster(monkeypatch) -> SpeakerFamilyCluster:
    """The profile's embedding in v4, reached through the ``speakers`` alias too."""
    fake = SpeakerFamilyCluster(
        {"speakers_v3": {}, "speakers_v4": {f"profile_{PROFILE_UUID}": {}, "other": {}}},
        aliases={"speakers": "speakers_v4"},
    )
    monkeypatch.setattr(ose, "_opensearch_client", lambda: fake)
    monkeypatch.setattr("app.services.opensearch_service.client.opensearch_client", fake)
    monkeypatch.setattr(
        "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
        lambda: ["speakers", "speakers_v3", "speakers_v4", "speakers_v3_backup"],
    )
    monkeypatch.setattr("app.services.profile_embedding_service.file_ids_in_scope", lambda o: [])
    monkeypatch.setattr(
        "app.services.profile_embedding_service.declined_file_ids", lambda db, ids: set()
    )
    monkeypatch.setattr(sc, "_invalidate_caches", lambda impact: None)
    return fake


def test_an_emptied_profile_is_erased_once_in_three_requests(cluster) -> None:
    impact = sc.SpeakerImpact(owner_ids={1}, profile_ids={PROFILE_ID})

    residual = sc.reconcile_speaker_impact(_db_with_an_emptied_profile(), impact)

    assert residual == []
    assert [call[0] for call in cluster.calls] == ["bulk", "refresh", "search"], cluster.calls
    assert cluster.docs["speakers_v4"] == {"other": {}}


def test_a_profile_that_survives_the_erase_is_still_a_residual(cluster) -> None:
    """CONTROL: fewer requests must not mean less proof."""
    cluster.write_blocked.add("speakers_v4")
    impact = sc.SpeakerImpact(owner_ids={1}, profile_ids={PROFILE_ID})

    residual = sc.reconcile_speaker_impact(_db_with_an_emptied_profile(), impact)

    assert residual == [
        {"stage": "profiles", "error": "1 profile embedding doc(s) survive in speakers_v4"}
    ]
