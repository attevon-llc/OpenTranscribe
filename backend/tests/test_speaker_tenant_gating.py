"""Speaker/voiceprint tenant-gate wiring regression tests (review findings F4/F5).

PR #250 plumbed ``organization_id`` through the OpenSearch speaker kNN service
signatures but the gate was never passed at the call sites, and the one-off
backfill stamped ALL of a member's speaker docs (including personal ones) by
``user_id``. These tests pin the fixes:

* an org file's speaker matching/stores carry the FILE's org (write + query);
* a personal file's speaker matching still runs with personal scope (None) —
  community invariance;
* the backfill stamps file-linked docs via ``media_file_id`` / profile docs via
  ``SpeakerProfile.organization_id`` and REMOVES stray org stamps from the same
  user's personal docs.

All tests are GPU-free and never touch a live OpenSearch cluster — the
OpenSearch functions/client are monkeypatched and only the emitted arguments /
query bodies are asserted (same pattern as ``test_tenant_isolation.py``).
"""

import uuid as uuid_pkg
from typing import Any

import numpy as np
import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User


# --------------------------------------------------------------------------- #
# Fixture: one user with an org file AND a personal file (+ speakers/profiles) #
# --------------------------------------------------------------------------- #
class World:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mk_user(db, label: str) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{label}_{uid}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, user: User, org_id: int | None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path=f"media/test/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _mk_speaker(db, *, user: User, media_file: MediaFile) -> Speaker:
    s = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=media_file.organization_id,
        media_file_id=media_file.id,
        # (user_id, media_file_id, name) is unique — suffix per speaker.
        name=f"SPEAKER_{uuid_pkg.uuid4().hex[:8]}",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _mk_cluster(db, *, user: User, speakers: list[Speaker], org_id: int | None = None):
    """Create a cluster with the given member speakers (legacy-style rows)."""
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember

    cluster = SpeakerCluster(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=org_id,
        member_count=len(speakers),
    )
    db.add(cluster)
    db.flush()
    for s in speakers:
        db.add(
            SpeakerClusterMember(
                uuid=uuid_pkg.uuid4(),
                cluster_id=cluster.id,
                speaker_id=s.id,
                confidence=0.9,
            )
        )
        s.cluster_id = cluster.id
    db.commit()
    db.refresh(cluster)
    return cluster


@pytest.fixture()
def world(db_session):
    """One org member owning an org file and a personal file (plus profiles)."""
    db = db_session
    org = Organization(
        external_org_id=f"org_gate_{uuid_pkg.uuid4().hex[:8]}", name="Gate Org", is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    user = _mk_user(db, "gate")
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:admin"))
    db.commit()

    org_file = _mk_file(db, user=user, org_id=org.id)
    personal_file = _mk_file(db, user=user, org_id=None)
    org_speaker = _mk_speaker(db, user=user, media_file=org_file)
    personal_speaker = _mk_speaker(db, user=user, media_file=personal_file)

    org_profile = SpeakerProfile(
        user_id=user.id,
        organization_id=org.id,
        name=f"Org Profile {uuid_pkg.uuid4().hex[:6]}",
    )
    personal_profile = SpeakerProfile(
        user_id=user.id,
        organization_id=None,
        name=f"Personal Profile {uuid_pkg.uuid4().hex[:6]}",
    )
    db.add_all([org_profile, personal_profile])
    db.commit()
    db.refresh(org_profile)
    db.refresh(personal_profile)

    return World(
        db=db,
        org=org,
        user=user,
        org_file=org_file,
        personal_file=personal_file,
        org_speaker=org_speaker,
        personal_speaker=personal_speaker,
        org_profile=org_profile,
        personal_profile=personal_profile,
    )


def _unit_embedding(dim: int = 256) -> np.ndarray:
    vec = np.ones(dim, dtype=np.float32)
    return vec / np.linalg.norm(vec)


# --------------------------------------------------------------------------- #
# F4 — matching pipeline: org threaded from the FILE into queries and stores   #
# --------------------------------------------------------------------------- #
class TestSpeakerMatchingOrgGate:
    def _patch_matching(self, monkeypatch) -> dict[str, list[Any]]:
        """Capture the org each kNN/store call receives; find no matches."""
        calls: dict[str, list[Any]] = {"find": [], "add": [], "profile_knn": []}

        def fake_find_matching_speaker(embedding, user_id, **kwargs):
            calls["find"].append(kwargs.get("organization_id"))
            return None

        def fake_add_speaker_embedding(**kwargs):
            calls["add"].append(kwargs.get("organization_id"))

        def fake_calculate_profile_similarity(embedding, user_id, **kwargs):
            calls["profile_knn"].append(kwargs.get("organization_id"))
            return []

        monkeypatch.setattr(
            "app.services.speaker_matching_service.find_matching_speaker",
            fake_find_matching_speaker,
        )
        monkeypatch.setattr(
            "app.services.speaker_matching_service.add_speaker_embedding",
            fake_add_speaker_embedding,
        )
        from app.services.profile_embedding_service import ProfileEmbeddingService

        monkeypatch.setattr(
            ProfileEmbeddingService,
            "calculate_profile_similarity",
            staticmethod(fake_calculate_profile_similarity),
        )
        return calls

    def test_org_file_matching_is_org_scoped_and_org_stamped(self, world, monkeypatch):
        """Processing an org file passes the FILE's org to every kNN query and
        stamps it on the stored voiceprint — org-A matching only sees org-A."""
        from app.services.speaker_matching_service import SpeakerMatchingService

        calls = self._patch_matching(monkeypatch)
        svc = SpeakerMatchingService(world.db, embedding_service=None)
        svc.process_speaker_embeddings_native(
            media_file_id=int(world.org_file.id),
            user_id=int(world.user.id),
            native_embeddings={int(world.org_speaker.id): _unit_embedding()},
        )

        assert calls["profile_knn"] == [world.org.id]
        # find_matching_speaker: once in match_speaker_to_known_speakers and
        # once in find_and_store_speaker_matches — both org-gated.
        assert calls["find"] and all(org == world.org.id for org in calls["find"])
        assert calls["add"] == [world.org.id]

    def test_personal_file_matching_stays_personal(self, world, monkeypatch):
        """Personal (org NULL) files behave exactly as before: every call runs
        with organization_id=None (community invariance)."""
        from app.services.speaker_matching_service import SpeakerMatchingService

        calls = self._patch_matching(monkeypatch)
        svc = SpeakerMatchingService(world.db, embedding_service=None)
        svc.process_speaker_embeddings_native(
            media_file_id=int(world.personal_file.id),
            user_id=int(world.user.id),
            native_embeddings={int(world.personal_speaker.id): _unit_embedding()},
        )

        assert calls["profile_knn"] == [None]
        assert calls["find"] and all(org is None for org in calls["find"])
        assert calls["add"] == [None]

    def test_unlabeled_matches_query_carries_file_org_gate(self, world, monkeypatch):
        """The raw cross-video kNN body is gated by the reference speaker's
        file org (term for org files, must_not-exists for personal ones)."""
        from app.services.speaker_matching_service import SpeakerMatchingService

        captured_bodies: list[dict] = []

        class FakeClient:
            def search(self, index=None, body=None):
                captured_bodies.append(body)
                return {"hits": {"hits": []}}

        monkeypatch.setattr("app.services.opensearch_service.opensearch_client", FakeClient())

        svc = SpeakerMatchingService(world.db, embedding_service=None)
        svc.find_unlabeled_speaker_matches(
            _unit_embedding(), int(world.user.id), int(world.org_speaker.id)
        )
        org_filters = captured_bodies[-1]["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
        assert {"term": {"organization_id": world.org.id}} in org_filters

        svc.find_unlabeled_speaker_matches(
            _unit_embedding(), int(world.user.id), int(world.personal_speaker.id)
        )
        personal_filters = captured_bodies[-1]["query"]["knn"]["embedding"]["filter"]["bool"][
            "filter"
        ]
        assert {"bool": {"must_not": {"exists": {"field": "organization_id"}}}} in personal_filters
        assert not any(
            "organization_id" in f.get("term", {}) for f in personal_filters if "term" in f
        )

    def test_profile_propagation_scoped_to_profile_org(self, world, monkeypatch):
        """Propagating an org profile only searches that org's speaker docs;
        a personal profile only searches personal docs."""
        from app.services.similarity_service import SimilarityService
        from app.services.speaker_matching_service import SpeakerMatchingService

        seen_orgs: list[Any] = []

        def fake_similarity_search(**kwargs):
            seen_orgs.append(kwargs.get("organization_id"))
            return []

        monkeypatch.setattr(
            SimilarityService, "opensearch_similarity_search", staticmethod(fake_similarity_search)
        )
        monkeypatch.setattr(
            SpeakerMatchingService,
            "_get_speaker_embedding_for_propagation",
            lambda self, sid: ([0.1] * 256, "fake-uuid"),
        )

        svc = SpeakerMatchingService(world.db, embedding_service=None)
        svc._propagate_profile_assignment(
            int(world.org_speaker.id), int(world.org_profile.id), int(world.user.id)
        )
        svc._propagate_profile_assignment(
            int(world.personal_speaker.id), int(world.personal_profile.id), int(world.user.id)
        )
        assert seen_orgs == [world.org.id, None]


# --------------------------------------------------------------------------- #
# F4 — smart suggestions: scope resolved from the speaker's file               #
# --------------------------------------------------------------------------- #
class TestSuggestionOrgGate:
    def test_consolidate_suggestions_scopes_by_file_org(self, world, monkeypatch):
        from unittest.mock import MagicMock

        from app.services.smart_speaker_suggestion_service import SmartSpeakerSuggestionService

        seen_orgs: list[Any] = []

        def fake_knn(client, embedding, user_id, threshold, **kwargs):
            seen_orgs.append(kwargs.get("organization_id"))
            return []

        monkeypatch.setattr(
            "app.services.smart_speaker_suggestion_service.get_speaker_embedding",
            lambda suuid: [0.1] * 256,
        )
        monkeypatch.setattr(
            "app.services.smart_speaker_suggestion_service._check_opensearch_profiles_exist",
            lambda *a, **kw: True,
        )
        monkeypatch.setattr(
            "app.services.smart_speaker_suggestion_service._execute_profile_knn_search",
            fake_knn,
        )
        monkeypatch.setattr("app.services.opensearch_service.opensearch_client", MagicMock())

        SmartSpeakerSuggestionService.consolidate_suggestions(
            int(world.org_speaker.id), int(world.user.id), world.db
        )
        SmartSpeakerSuggestionService.consolidate_suggestions(
            int(world.personal_speaker.id), int(world.user.id), world.db
        )
        assert seen_orgs == [world.org.id, None]

    def test_profile_knn_filter_bodies(self):
        """The profile kNN body encodes the tenant gate (pure filter check)."""
        from app.services.smart_speaker_suggestion_service import _execute_profile_knn_search

        captured: list[dict] = []

        class FakeClient:
            def search(self, index=None, body=None):
                captured.append(body)
                return {"hits": {"hits": []}}

        _execute_profile_knn_search(
            FakeClient(), np.array([0.1] * 256), user_id=7, threshold=0.5, organization_id=42
        )
        org_must = captured[-1]["query"]["knn"]["embedding"]["filter"]["bool"]["must"]
        assert {"term": {"organization_id": 42}} in org_must

        _execute_profile_knn_search(
            FakeClient(), np.array([0.1] * 256), user_id=7, threshold=0.5, organization_id=None
        )
        personal_must = captured[-1]["query"]["knn"]["embedding"]["filter"]["bool"]["must"]
        assert {"bool": {"must_not": {"exists": {"field": "organization_id"}}}} in personal_must


# --------------------------------------------------------------------------- #
# F4/#262 — clustering: cluster kNN gated + cluster rows/docs tenant-stamped   #
# --------------------------------------------------------------------------- #
class TestClusteringOrgGate:
    def test_find_or_create_cluster_passes_file_org(self, world, monkeypatch):
        from app.services.speaker_clustering_service import SpeakerClusteringService

        seen_orgs: list[Any] = []
        stored_orgs: list[Any] = []

        def fake_find_matching_clusters(embedding, user_id, **kwargs):
            seen_orgs.append(kwargs.get("organization_id"))
            return []

        def fake_store_cluster_embedding(**kwargs):
            stored_orgs.append(kwargs.get("organization_id"))
            return True

        monkeypatch.setattr(
            "app.services.opensearch_service.find_matching_clusters",
            fake_find_matching_clusters,
        )
        monkeypatch.setattr(
            "app.services.opensearch_service.store_cluster_embedding",
            fake_store_cluster_embedding,
        )

        svc = SpeakerClusteringService(world.db)
        emb = [float(x) for x in _unit_embedding()]
        cluster_org = svc.find_or_create_cluster(int(world.org_speaker.id), int(world.user.id), emb)
        cluster_personal = svc.find_or_create_cluster(
            int(world.personal_speaker.id), int(world.user.id), emb
        )

        assert seen_orgs == [world.org.id, None]
        # No centroid matched (gate) -> both became singleton clusters, each
        # stamped with ITS file's tenant scope (row + centroid doc).
        assert cluster_org is not None and cluster_personal is not None
        assert cluster_org.organization_id == world.org.id
        assert cluster_personal.organization_id is None
        assert stored_orgs == [world.org.id, None]

    def test_find_matching_clusters_query_carries_tenant_gate(self, monkeypatch):
        """The cluster kNN body encodes the tenant gate: a term filter for org
        scope, the must_not-exists clause for personal scope."""
        from app.services.opensearch_service import find_matching_clusters

        captured: list[dict] = []

        class FakeClient:
            def search(self, index=None, body=None):
                captured.append(body)
                return {"hits": {"hits": []}}

        # The package's own modules resolve these through the module that owns
        # them, so patch there — patching the package facade only rebinds the
        # re-export, not what ``clusters.find_matching_clusters`` reads.
        monkeypatch.setattr(
            "app.services.opensearch_service.client.opensearch_client", FakeClient()
        )
        monkeypatch.setattr(
            "app.services.opensearch_service.clusters.get_active_speaker_index",
            lambda: "speakers",
        )

        find_matching_clusters([0.1] * 256, user_id=7, organization_id=42)
        org_filters = captured[-1]["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
        assert {"term": {"organization_id": 42}} in org_filters
        assert {"term": {"document_type": "cluster"}} in org_filters

        find_matching_clusters([0.1] * 256, user_id=7, organization_id=None)
        personal_filters = captured[-1]["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
        assert {"bool": {"must_not": {"exists": {"field": "organization_id"}}}} in personal_filters
        assert not any(
            "organization_id" in f.get("term", {}) for f in personal_filters if "term" in f
        )

    def test_store_cluster_embedding_stamps_org_only_when_present(self, monkeypatch):
        """Org clusters carry organization_id on the centroid doc; personal /
        community docs have NO org field (byte-identical to pre-tenant docs)."""
        import app.services.opensearch_service as oss
        from app.services.opensearch_service import client as oss_client
        from app.services.opensearch_service import clusters as oss_clusters

        captured: list[dict] = []

        class FakeClient:
            def index(self, index=None, body=None, **kwargs):
                captured.append(body)
                return {"result": "created"}

        monkeypatch.setattr(oss_client, "opensearch_client", FakeClient())
        monkeypatch.setattr(oss_clusters, "get_active_speaker_index", lambda: "speakers")
        monkeypatch.setattr(oss_clusters, "_indices_verified", True)

        assert oss.store_cluster_embedding("uuid-org", 7, [0.1] * 256, organization_id=42)
        assert captured[-1]["organization_id"] == 42
        assert captured[-1]["document_type"] == "cluster"

        assert oss.store_cluster_embedding("uuid-personal", 7, [0.1] * 256)
        assert "organization_id" not in captured[-1]

    def _patch_batch_recluster(self, monkeypatch, embeddings_by_uuid: dict[str, list[float]]):
        """Stub every OpenSearch touchpoint of batch_recluster."""
        stored_orgs: list[Any] = []

        def fake_iter_speaker_embeddings(user_id, speaker_uuids=None, batch_size=200):
            batch = [
                {"speaker_uuid": u, "embedding": embeddings_by_uuid[u]}
                for u in (speaker_uuids or [])
                if u in embeddings_by_uuid
            ]
            if batch:
                yield batch

        def fake_store_cluster_embedding(**kwargs):
            stored_orgs.append(kwargs.get("organization_id"))
            return True

        monkeypatch.setattr(
            "app.services.opensearch_service.iter_speaker_embeddings",
            fake_iter_speaker_embeddings,
        )
        monkeypatch.setattr(
            "app.services.opensearch_service.store_cluster_embedding",
            fake_store_cluster_embedding,
        )
        monkeypatch.setattr(
            "app.services.opensearch_service.delete_cluster_embedding", lambda u: True
        )
        monkeypatch.setattr("app.services.opensearch_service.opensearch_client", None)
        return stored_orgs

    def test_batch_recluster_partitions_similarity_graph_per_tenant(self, world, monkeypatch):
        """Identical voices on an org file and a personal file must NOT merge:
        the Phase-2 AHC graph is partitioned per tenant scope, and each
        resulting cluster is stamped with its partition's org."""
        from app.models.media import SpeakerCluster
        from app.services.speaker_clustering_service import SpeakerClusteringService

        db = world.db
        org_speaker2 = _mk_speaker(db, user=world.user, media_file=world.org_file)
        personal_speaker2 = _mk_speaker(db, user=world.user, media_file=world.personal_file)
        speakers = [world.org_speaker, org_speaker2, world.personal_speaker, personal_speaker2]

        # All four speakers share ONE voice — unpartitioned AHC would group
        # them into a single cross-tenant cluster.
        emb = [float(x) for x in _unit_embedding()]
        stored_orgs = self._patch_batch_recluster(monkeypatch, {str(s.uuid): emb for s in speakers})

        svc = SpeakerClusteringService(db)
        result = svc.batch_recluster(int(world.user.id))

        assert result["similarity_clusters"] == 2
        clusters = db.query(SpeakerCluster).filter(SpeakerCluster.user_id == world.user.id).all()
        by_org = {c.organization_id: c for c in clusters}
        assert set(by_org) == {world.org.id, None}

        org_members = {s.id for s in speakers if s.cluster_id == by_org[world.org.id].id}
        personal_members = {s.id for s in speakers if s.cluster_id == by_org[None].id}
        assert org_members == {world.org_speaker.id, org_speaker2.id}
        assert personal_members == {world.personal_speaker.id, personal_speaker2.id}

        # Each centroid doc carries its partition's scope.
        assert sorted(stored_orgs, key=str) == sorted([world.org.id, None], key=str)

    def test_batch_recluster_all_personal_unchanged(self, world, monkeypatch):
        """Community invariance: with org NULL everywhere there is exactly one
        partition and the grouping matches the pre-tenant behavior (one
        cluster, no org stamp anywhere)."""
        from app.models.media import SpeakerCluster
        from app.services.speaker_clustering_service import SpeakerClusteringService

        db = world.db
        user = _mk_user(db, "community")
        personal_file = _mk_file(db, user=user, org_id=None)
        speakers = [_mk_speaker(db, user=user, media_file=personal_file) for _ in range(3)]

        emb = [float(x) for x in _unit_embedding()]
        stored_orgs = self._patch_batch_recluster(monkeypatch, {str(s.uuid): emb for s in speakers})

        svc = SpeakerClusteringService(db)
        result = svc.batch_recluster(int(user.id))

        assert result["similarity_clusters"] == 1
        assert result["similarity_speakers_assigned"] == 3
        clusters = db.query(SpeakerCluster).filter(SpeakerCluster.user_id == user.id).all()
        assert len(clusters) == 1
        assert clusters[0].organization_id is None
        assert stored_orgs == [None]


# --------------------------------------------------------------------------- #
# F5 — backfill: per-file/per-profile stamping + personal repair               #
# --------------------------------------------------------------------------- #
class FakeIndices:
    def exists(self, index=None):
        return True


class FakeOSClient:
    """Captures update_by_query calls issued by the backfill."""

    def __init__(self):
        self.indices = FakeIndices()
        self.calls: list[dict] = []

    def update_by_query(self, index=None, body=None, **kwargs):
        self.calls.append({"index": index, "body": body})
        return {"updated": 1}


def _stamp_calls(client: FakeOSClient) -> list[dict]:
    return [c for c in client.calls if "params" in c["body"]["script"]]


def _remove_calls(client: FakeOSClient) -> list[dict]:
    return [c for c in client.calls if "remove" in c["body"]["script"]["source"]]


class TestSpeakerBackfill:
    def test_scope_maps_split_org_and_personal(self, world):
        """The DB mapping keys per-file docs by the FILE's org and keeps the
        same member's personal file/profile in the repair lists."""
        from app.tasks.tenant_backfill_task import _build_speaker_scope_maps

        scope = _build_speaker_scope_maps(world.db)

        assert world.org_file.id in scope.org_to_file_ids.get(world.org.id, [])
        assert world.personal_file.id not in scope.org_to_file_ids.get(world.org.id, [])
        assert world.personal_file.id in scope.personal_file_ids

        assert world.org_profile.id in scope.org_to_profile_ids.get(world.org.id, [])
        assert world.personal_profile.id not in scope.org_to_profile_ids.get(world.org.id, [])
        assert world.personal_profile.id in scope.personal_profile_ids

        assert world.user.id in scope.member_user_ids

    def test_backfill_stamps_file_docs_but_not_personal_docs(self):
        """A file-linked doc gets the org stamp; the same user's personal doc
        is never stamped — instead any stray org field on it is removed."""
        from app.tasks.tenant_backfill_task import SpeakerScopeMaps
        from app.tasks.tenant_backfill_task import _backfill_speaker_docs

        client = FakeOSClient()
        scope = SpeakerScopeMaps(
            org_to_file_ids={7: [101]},
            org_to_profile_ids={7: [201]},
            member_user_ids=[31],
            personal_file_ids=[102],
            personal_profile_ids=[202],
            org_to_cluster_uuids={7: ["cluster-org"]},
            personal_cluster_uuids=["cluster-personal"],
        )
        updated = _backfill_speaker_docs(client, scope)
        assert updated > 0

        stamps = _stamp_calls(client)
        removes = _remove_calls(client)
        assert stamps and removes

        # Every stamp is keyed on media_file_id / profile_id / cluster_uuid —
        # never user_id.
        for call in stamps:
            filters = call["body"]["query"]["bool"]["filter"]
            keyed = [f for f in filters if "terms" in f]
            assert keyed, f"stamp not id-keyed: {call}"
            for f in keyed:
                assert "user_id" not in f["terms"]
            assert call["body"]["script"]["params"]["org_id"] == 7

        # Org file id is stamped; the personal file id never is.
        stamped_file_ids = {
            fid
            for call in stamps
            for f in call["body"]["query"]["bool"]["filter"]
            for fid in f.get("terms", {}).get("media_file_id", [])
        }
        assert 101 in stamped_file_ids
        assert 102 not in stamped_file_ids

        # Org profile stamped; personal profile only in the repair pass.
        stamped_profile_ids = {
            pid
            for call in stamps
            for f in call["body"]["query"]["bool"]["filter"]
            for pid in f.get("terms", {}).get("profile_id", [])
        }
        assert 201 in stamped_profile_ids
        assert 202 not in stamped_profile_ids

        # Org cluster stamped by cluster_uuid; personal cluster never stamped.
        stamped_cluster_uuids = {
            cu
            for call in stamps
            for f in call["body"]["query"]["bool"]["filter"]
            for cu in f.get("terms", {}).get("cluster_uuid", [])
        }
        assert "cluster-org" in stamped_cluster_uuids
        assert "cluster-personal" not in stamped_cluster_uuids

        # Repair passes only touch docs that still carry an org field, and
        # cluster repair is keyed on cluster_uuid — the old user_id-wide
        # cluster strip is gone (it would erase the org stamps just written).
        removed_file_ids: set[int] = set()
        removed_profile_ids: set[int] = set()
        removed_cluster_uuids: set[str] = set()
        for call in removes:
            filters = call["body"]["query"]["bool"]["filter"]
            assert {"exists": {"field": "organization_id"}} in filters
            for f in filters:
                removed_file_ids.update(f.get("terms", {}).get("media_file_id", []))
                removed_profile_ids.update(f.get("terms", {}).get("profile_id", []))
                removed_cluster_uuids.update(f.get("terms", {}).get("cluster_uuid", []))
                assert "user_id" not in f.get("terms", {})
        assert 102 in removed_file_ids
        assert 202 in removed_profile_ids
        assert removed_cluster_uuids == {"cluster-personal"}

    def test_backfill_stamp_queries_are_idempotent(self):
        """Stamp queries skip docs already carrying the correct org."""
        from app.tasks.tenant_backfill_task import SpeakerScopeMaps
        from app.tasks.tenant_backfill_task import _backfill_speaker_docs

        client = FakeOSClient()
        _backfill_speaker_docs(
            client,
            SpeakerScopeMaps(org_to_file_ids={9: [11]}, org_to_cluster_uuids={9: ["cu"]}),
        )
        stamps = _stamp_calls(client)
        assert stamps
        for call in stamps:
            assert {"term": {"organization_id": 9}} in call["body"]["query"]["bool"]["must_not"]


# --------------------------------------------------------------------------- #
# #262 — cluster backfill: rows + docs stamped from MEMBER speakers' file orgs #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cluster_world(world):
    """Three legacy clusters for one member: all-org, mixed, all-personal."""
    db = world.db
    org_speaker2 = _mk_speaker(db, user=world.user, media_file=world.org_file)
    personal_speaker2 = _mk_speaker(db, user=world.user, media_file=world.personal_file)

    # Legacy rows: NULL org everywhere; the mixed one even carries a stray
    # stamp (simulating pre-repair damage) that must be reset to NULL.
    org_cluster = _mk_cluster(db, user=world.user, speakers=[world.org_speaker, org_speaker2])
    mixed_cluster = _mk_cluster(
        db,
        user=world.user,
        speakers=[personal_speaker2, _mk_speaker(db, user=world.user, media_file=world.org_file)],
        org_id=world.org.id,
    )
    personal_cluster = _mk_cluster(db, user=world.user, speakers=[world.personal_speaker])
    empty_cluster = _mk_cluster(db, user=world.user, speakers=[])

    return World(
        **world.__dict__,
        org_cluster=org_cluster,
        mixed_cluster=mixed_cluster,
        personal_cluster=personal_cluster,
        empty_cluster=empty_cluster,
    )


class TestClusterBackfill:
    def test_scope_maps_resolve_cluster_scopes(self, cluster_world):
        """All-same-org members -> org list; mixed/personal/empty -> personal
        list; mixed clusters are counted (left NULL by design)."""
        from app.tasks.tenant_backfill_task import _build_speaker_scope_maps

        w = cluster_world
        scope = _build_speaker_scope_maps(w.db)

        assert w.org_cluster.id in scope.org_to_cluster_ids.get(w.org.id, [])
        assert str(w.org_cluster.uuid) in scope.org_to_cluster_uuids.get(w.org.id, [])

        for cluster in (w.mixed_cluster, w.personal_cluster, w.empty_cluster):
            assert cluster.id in scope.personal_cluster_ids
            assert str(cluster.uuid) in scope.personal_cluster_uuids
            assert cluster.id not in scope.org_to_cluster_ids.get(w.org.id, [])

        assert scope.mixed_cluster_count == 1

    def test_stamp_cluster_rows_syncs_db(self, cluster_world):
        """Row stamping: all-org cluster gains the org; the mixed cluster's
        stray stamp is reset to NULL; personal/empty rows stay NULL."""
        from app.tasks.tenant_backfill_task import _build_speaker_scope_maps
        from app.tasks.tenant_backfill_task import _stamp_cluster_rows

        w = cluster_world
        scope = _build_speaker_scope_maps(w.db)
        changed = _stamp_cluster_rows(w.db, scope)
        assert changed == 2  # org stamp + mixed reset

        for cluster in (w.org_cluster, w.mixed_cluster, w.personal_cluster, w.empty_cluster):
            w.db.refresh(cluster)
        assert w.org_cluster.organization_id == w.org.id
        assert w.mixed_cluster.organization_id is None
        assert w.personal_cluster.organization_id is None
        assert w.empty_cluster.organization_id is None

        # Idempotent: a second run changes nothing.
        assert _stamp_cluster_rows(w.db, _build_speaker_scope_maps(w.db)) == 0

    def test_backfill_docs_stamp_org_clusters_and_strip_the_rest(self, cluster_world):
        """Doc backfill mirrors the row resolution: the all-org cluster doc is
        stamped; mixed/personal/empty cluster docs get the org field removed."""
        from app.tasks.tenant_backfill_task import _backfill_speaker_docs
        from app.tasks.tenant_backfill_task import _build_speaker_scope_maps

        w = cluster_world
        scope = _build_speaker_scope_maps(w.db)
        client = FakeOSClient()
        _backfill_speaker_docs(client, scope)

        cluster_calls = [
            c
            for c in client.calls
            if {"term": {"document_type": "cluster"}} in c["body"]["query"]["bool"]["filter"]
        ]
        stamped = {
            cu
            for c in cluster_calls
            if "params" in c["body"]["script"]
            for f in c["body"]["query"]["bool"]["filter"]
            for cu in f.get("terms", {}).get("cluster_uuid", [])
        }
        stripped = {
            cu
            for c in cluster_calls
            if "remove" in c["body"]["script"]["source"]
            for f in c["body"]["query"]["bool"]["filter"]
            for cu in f.get("terms", {}).get("cluster_uuid", [])
        }
        assert stamped == {str(w.org_cluster.uuid)}
        assert stripped == {
            str(w.mixed_cluster.uuid),
            str(w.personal_cluster.uuid),
            str(w.empty_cluster.uuid),
        }
