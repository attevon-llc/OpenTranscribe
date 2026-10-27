"""A purge selects voiceprint documents by what Postgres says is in its scope, not by their stamp.

A document written before the tenant backfill carries no ``organization_id``; a document can
also carry the WRONG one. Selecting by the stamp then misses an unstamped organization document
in the organization's own purge, and lets a member's personal purge delete it. These tests seed
deliberately inconsistent documents and clusters and assert exactly which survive each scope's
purge, and that the audit counts are what the store reported deleted.

Real Postgres rows; the speaker index is the in-memory ``FakeOpenSearch``; the centroid rebuild
of a cluster that also holds another tenant's speakers runs through ``speaker_os_seams``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest

from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import VOICE_B
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


class _KeepOpen:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _capabilities():
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.defaults": True}
    )
    clear_write_backstop_cache()
    yield
    reset_capability_resolver()
    clear_write_backstop_cache()


@pytest.fixture()
def index(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    return fake


def _world(gov, index, speaker_os_seams) -> SimpleNamespace:
    """Org A, a personal workspace and org B, with documents that do not agree with their rows."""
    db = gov.db

    def speaker(owner, org):
        row = make_speaker(db, owner, gov.file(owner, org=org))
        return row

    a1 = speaker(gov.alice, gov.org)  # org A, document UNSTAMPED (predates the backfill)
    a2 = speaker(gov.bob, gov.org)  # org A, document stamped correctly
    personal = speaker(gov.alice, None)  # personal, document unstamped
    personal_misstamped = speaker(gov.alice, None)  # personal, document stamped with org A
    b1 = speaker(gov.outsider, gov.other_org)  # org B, stamped
    profile_a = make_profile(db, gov.alice, "Ann")
    profile_a.organization_id = gov.org.id
    profile_personal = make_profile(db, gov.alice, "Pia")
    all_a = make_cluster(db, gov.alice, a1, a2, label="all-in-a")  # row unstamped: legacy
    cross = make_cluster(db, gov.alice, a1, b1, label="cross-tenant")  # members in two tenants
    db.commit()

    index.add(str(a1.uuid), gov.alice.id, None)
    index.add(str(a2.uuid), gov.bob.id, gov.org.id)
    index.add(str(personal.uuid), gov.alice.id, None)
    index.add(str(personal_misstamped.uuid), gov.alice.id, gov.org.id)
    index.add(str(b1.uuid), gov.outsider.id, gov.other_org.id)
    index.add(f"profile_{profile_a.uuid}", gov.alice.id, None)
    index.add(f"profile_{profile_personal.uuid}", gov.alice.id, None)
    index.add(f"cluster_{all_a.uuid}", gov.alice.id, None)
    index.add(f"cluster_{cross.uuid}", gov.alice.id, None)
    speaker_os_seams["voiceprints"].update({str(a1.uuid): VOICE_A, str(b1.uuid): VOICE_B})
    return SimpleNamespace(
        a1=a1,
        a2=a2,
        personal=personal,
        personal_misstamped=personal_misstamped,
        b1=b1,
        profile_a=profile_a,
        profile_personal=profile_personal,
        all_a=all_a,
        cross=cross,
    )


def _ids(world, *names) -> set[str]:
    out: set[str] = set()
    for name in names:
        row = getattr(world, name)
        prefix = (
            "profile_"
            if name.startswith("profile")
            else "cluster_"
            if name in ("all_a", "cross")
            else ""
        )
        out.add(f"{prefix}{row.uuid}")
    return out


def _run(gov, scope):
    return purge.run_purge_pass(
        scope,
        ["voiceprints"],
        session_factory=lambda: cast("Session", _KeepOpen(gov.db)),
        reindex=lambda files: 0,
    )


def test_an_organization_purge_removes_its_unstamped_documents_and_only_its_own(
    gov, index, speaker_os_seams
):
    world = _world(gov, index, speaker_os_seams)

    result = _run(gov, purge.PurgeScope("organization", organization_id=gov.org.id))

    survivors = index.ids()
    gone = _ids(world, "a1", "a2", "profile_a", "all_a")
    assert not (gone & survivors), f"not removed: {sorted(gone & survivors)}"
    assert _ids(world, "personal", "profile_personal", "cross", "b1") <= survivors
    assert str(world.personal_misstamped.uuid) in survivors, (
        "a personal document that carries the organization's stamp by mistake is not the "
        "organization's: it is selected by its speaker's file, not by the stamp"
    )
    assert result.errors == []


def test_a_members_personal_purge_does_not_reach_an_unstamped_organization_document(
    gov, index, speaker_os_seams
):
    world = _world(gov, index, speaker_os_seams)

    result = _run(gov, purge.PurgeScope("user", user_id=gov.alice.id, organization_id=None))

    survivors = index.ids()
    assert str(world.a1.uuid) in survivors, "alice's ORGANIZATION speaker's document survives"
    assert _ids(world, "a2", "b1", "profile_a", "all_a") <= survivors
    gone = _ids(world, "personal", "personal_misstamped", "profile_personal")
    assert not (gone & survivors)
    assert result.errors == []


def test_a_cluster_holding_two_tenants_is_trimmed_not_deleted(gov, index, speaker_os_seams):
    world = _world(gov, index, speaker_os_seams)
    cross_id, b1_id, a1_id = world.cross.id, world.b1.id, world.a1.id

    result = _run(gov, purge.PurgeScope("organization", organization_id=gov.org.id))

    gov.db.expire_all()
    members = {
        m.speaker_id
        for m in gov.db.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.cluster_id == cross_id
        )
    }
    assert members == {b1_id}, "only the in-scope member left"
    cluster = gov.db.get(SpeakerCluster, cross_id)
    assert cluster is not None and cluster.member_count == 1
    assert gov.db.get(Speaker, a1_id).cluster_id is None
    assert gov.db.get(Speaker, b1_id).cluster_id == cross_id
    centroid = speaker_os_seams["clusters"][str(world.cross.uuid)]
    assert cosine(centroid, VOICE_B) > 0.9999, "rebuilt from the member that stayed"
    assert result.counts()["postgres"]["cross_scope_clusters_trimmed"] == 1
    assert f"cluster_{world.cross.uuid}" in index.ids(), (
        "the other tenant's centroid is not deleted"
    )


def test_the_counts_are_what_the_store_reported_deleted_not_the_ids_asked_for(
    gov, index, speaker_os_seams
):
    world = _world(gov, index, speaker_os_seams)
    # Ask for more than exists: one of the scope's speakers has no document at all.
    index.docs = [d for d in index.docs if d["id"] != str(world.a2.uuid)]

    result = _run(gov, purge.PurgeScope("organization", organization_id=gov.org.id))

    assert result.counts()["opensearch"] == {
        "speaker_docs_deleted": 1,  # a1 only; a2's was never indexed
        "profile_docs_deleted": 1,
        "cluster_docs_deleted": 1,
    }


def test_a_second_pass_over_a_purged_scope_deletes_nothing_and_stays_clean(
    gov, index, speaker_os_seams
):
    _world(gov, index, speaker_os_seams)
    scope = purge.PurgeScope("organization", organization_id=gov.org.id)
    _run(gov, scope)

    again = _run(gov, scope)

    assert again.counts()["opensearch"] == {
        "speaker_docs_deleted": 0,
        "profile_docs_deleted": 0,
        "cluster_docs_deleted": 0,
    }
    assert again.residual().ok


# --- the personal-scope kNN gate ----------------------------------------------------------


def _personal_hits(index, user_id, **clause_args) -> set[str]:
    """Documents a personal-scope voiceprint search for ``user_id`` would be allowed to match."""
    from app.services.opensearch_service.client import _speaker_org_filter_clauses

    query = {
        "bool": {
            "filter": [
                {"term": {"user_id": user_id}},
                *_speaker_org_filter_clauses(None, **clause_args),
            ]
        }
    }
    return {d["id"] for d in index.docs if index._matches(d, query)}


def test_a_personal_search_does_not_match_an_unstamped_organization_files_voiceprint(
    gov, index, monkeypatch
):
    from app.models.media import MediaFile
    from app.services.opensearch_service import client as os_client

    org_file = gov.file(gov.alice, org=gov.org)
    personal_file = gov.file(gov.alice, org=None)
    index.add("org-file-doc-unstamped", gov.alice.id, None, media_file_id=org_file.id)
    index.add("personal-file-doc", gov.alice.id, None, media_file_id=personal_file.id)
    index.add("org-file-doc-stamped", gov.alice.id, gov.org.id, media_file_id=org_file.id)
    monkeypatch.setattr(
        os_client,
        "_org_file_ids_of",
        lambda user_ids: [
            int(r[0])
            for r in gov.db.query(MediaFile.id).filter(
                MediaFile.user_id.in_(list(user_ids)), MediaFile.organization_id.isnot(None)
            )
        ],
    )

    without = _personal_hits(index, gov.alice.id)
    with_gate = _personal_hits(index, gov.alice.id, user_ids=[gov.alice.id])

    assert "org-file-doc-unstamped" in without, "control: the stamp alone lets it through"
    assert with_gate == {"personal-file-doc"}


def test_the_personal_gate_fails_closed_when_the_organization_files_cannot_be_listed(
    gov, index, monkeypatch
):
    from app.services.opensearch_service import client as os_client

    index.add("personal-file-doc", gov.alice.id, None, media_file_id=gov.file(gov.alice, None).id)
    monkeypatch.setattr(os_client, "_org_file_ids_of", lambda user_ids: None)

    assert _personal_hits(index, gov.alice.id, user_ids=[gov.alice.id]) == set()


def _filters_of(search_body: dict) -> str:
    """The filter clauses a recorded kNN request carried, as text to look for a clause in."""
    import json

    return search_body if isinstance(search_body, str) else json.dumps(search_body)


@pytest.fixture()
def recording_cluster(monkeypatch):
    """A recording search client plus a patched organization-file listing."""
    from unittest.mock import MagicMock

    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import matching

    client = MagicMock(name="opensearch")
    client.search.return_value = {"hits": {"hits": []}}
    client.msearch.return_value = {"responses": [{"hits": {"hits": []}}]}
    monkeypatch.setattr(os_client, "opensearch_client", client)
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", client)
    monkeypatch.setattr(matching, "ensure_indices_exist", lambda: None)
    monkeypatch.setattr(matching, "query_embedding_dimension_mismatch", lambda *a: False)
    monkeypatch.setattr(os_client, "_org_file_ids_of", lambda user_ids: [901, 902])
    return client


def test_every_personal_speaker_search_carries_the_organization_file_exclusion(
    recording_cluster, monkeypatch, db_session, normal_user
):
    """The gate is only worth anything if each speaker kNN read passes the owners to it."""
    from app.services import similarity_service
    from app.services.opensearch_service import matching
    from app.services.speaker_matching_service import SpeakerMatchingService

    embedding = [0.1, 0.2, 0.3, 0.4]
    matching.find_matching_speaker(embedding, normal_user.id, organization_id=None)
    matching.batch_find_matching_speakers(
        [{"id": 1, "embedding": embedding}], normal_user.id, organization_id=None
    )
    matching.msearch_speaker_similarities(
        [{"speaker_uuid": "u", "embedding": embedding}], normal_user.id, organization_id=None
    )
    similarity_service.SimilarityService.opensearch_similarity_search(
        embedding, normal_user.id, organization_id=None
    )
    import numpy as np

    built = SpeakerMatchingService(db_session, None)._build_speaker_match_query(
        np.array(embedding), normal_user.id, 5, None
    )

    SpeakerMatchingService(db_session, None).find_unlabeled_speaker_matches(
        np.array(embedding), normal_user.id, 987_654
    )

    bodies = [c.kwargs["body"] for c in recording_cluster.search.call_args_list]
    bodies += [c.kwargs["body"] for c in recording_cluster.msearch.call_args_list]
    bodies.append(built)
    assert len(bodies) == 6
    for body in bodies:
        assert '"media_file_id": [901, 902]' in _filters_of(body), body
