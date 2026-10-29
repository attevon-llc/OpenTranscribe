"""The privacy planes against a REAL OpenSearch: label resync, drift detection, purge residual.

``tests/unit/test_privacy_purge_opensearch_scope.py`` and ``tests/api/test_privacy_switch_relabels_index.py``
drive the same code against an in-memory ``FakeOpenSearch`` and a recorder for the re-index
dispatch. They prove the decisions; they cannot prove that the queries the code SENDS are
accepted by a real cluster, that a terms aggregation over the real ``speaker`` mapping returns
what ``indexed_speaker_labels`` expects, or that a ``delete_by_query`` over an alias, its index
and the legacy backup counts a document once. Before this file those were checked by hand in
Wave 3 of every release.

Everything here runs on THROWAWAY indices (uuid4-named, deleted in teardown) so the live
deployment's indices are never read or written. Rows are created in the test session's
savepoint and roll back with it.

Each case has its negative control: a clean index reports no drift, a survivor outside the
purged scope survives, and an unreachable store reads as UNMEASURED rather than as empty.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.media import TranscriptSegment
from app.services.privacy import label_reindex
from app.services.privacy.label_reindex import ResyncScope
from app.services.privacy.purge import PurgeScope
from app.services.privacy.purge_voiceprints import VoiceprintTargets
from app.services.privacy.purge_voiceprints import org_stamp_clause
from app.services.privacy.purge_voiceprints import purge_voiceprint_docs
from app.services.search.reindex_dispatch import ReindexDispatch
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true",
        reason="No OpenSearch reachable (SKIP_OPENSEARCH): these cases need a real cluster.",
    ),
]

VOICE_NAME = "Zylofenix Quorra"


@pytest.fixture
def client():
    """The package-level client the privacy code reads, unpatched: this is the real cluster."""
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service import opensearch_client

    live = get_opensearch_client()
    # Asserts, not skips: the module-level skipif already established a cluster is reachable.
    assert live is not None, "SKIP_OPENSEARCH reported a cluster but the client factory gave None"
    assert opensearch_client is not None, "the package-level client was not built at import"
    return live


@pytest.fixture
def chunk_index(client, monkeypatch) -> Iterator[str]:
    """A throwaway chunks index with the REAL mapping (``speaker`` is a keyword)."""
    from app.core.config import settings
    from app.services.search import indexing_service as svc

    name = f"test_priv_chunks_{uuid.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    try:
        yield name
    finally:
        client.indices.delete(index=name, ignore=[404])


_SPEAKER_MAPPING = {
    "mappings": {
        "properties": {
            "user_id": {"type": "integer"},
            "organization_id": {"type": "integer"},
            "document_type": {"type": "keyword"},
        }
    }
}


@pytest.fixture
def speaker_indices(client, monkeypatch) -> Iterator[dict[str, str]]:
    """Throwaway v3, v4 and legacy-backup speaker indices plus the alias over v4.

    Mirrors the shapes the purge has to sweep: the alias and the index behind it (counted
    once), and the ``_v3_backup`` index a startup restore would copy back into v3.
    """
    from app.core.config import settings

    base = f"test_priv_spk_{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", base)
    names = {"alias": base, "v3": f"{base}_v3", "v4": f"{base}_v4", "backup": f"{base}_v3_backup"}
    for key in ("v3", "v4", "backup"):
        client.indices.create(index=names[key], body=_SPEAKER_MAPPING)
    client.indices.put_alias(index=names["v4"], name=names["alias"])
    try:
        yield names
    finally:
        for key in ("v3", "v4", "backup"):
            client.indices.delete(index=names[key], ignore=[404])


def _put(client, index: str, doc_id: str, **fields: Any) -> None:
    client.index(index=index, id=doc_id, body=fields, refresh=True)


def _present(client, speaker_indices: dict[str, str], doc_id: str) -> int:
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids

    found = count_speaker_docs_by_ids([doc_id])
    assert found is not None, "the residual count could not be measured against a live cluster"
    return found


# --- labels: resync selection and drift, on the real chunk index ---------------------------


def _voice_named_file(db: Session, owner: Any) -> tuple[Any, Any]:
    media_file = make_file(db, owner)
    speaker = make_speaker(
        db,
        owner,
        media_file,
        suggested_name=VOICE_NAME,
        suggestion_source="voice_match",
        confidence=0.91,
    )
    db.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.0,
            text="seeded segment",
        )
    )
    db.commit()
    return media_file, speaker


def _index_chunk(client, index: str, media_file: Any, label: str) -> None:
    _put(
        client,
        index,
        f"{media_file.uuid}_0",
        file_uuid=str(media_file.uuid),
        chunk_index=0,
        content="seeded chunk",
        title="t",
        speaker=label,
        speaker_id=1,
        speakers=[label],
        tags=[],
        content_type="audio/wav",
        accessible_user_ids=[1],
        upload_time="2026-10-28T00:00:00+00:00",
        language="en",
        start_time=0.0,
        end_time=1.0,
        indexed_at="2026-10-28T00:00:00+00:00",
    )


class _Recorder:
    def __init__(self) -> None:
        self.reindexed: list[int] = []
        self.facts: list[int] = []

    def reindex(self, *, file_id, file_uuid, user_id, priority=None):
        self.reindexed.append(int(file_id))
        return ReindexDispatch.QUEUED

    def facts_for(self, file_id: int) -> None:
        self.facts.append(int(file_id))


def _scan_one(db: Session, media_file: Any, rec: _Recorder):
    """Run the real drift scan over exactly this file (the cursor is just below its id)."""
    from app.services.system_settings_service import set_setting

    set_setting(db, label_reindex.DRIFT_CURSOR_KEY, media_file.id - 1, "test cursor")
    return label_reindex.detect_label_drift(db, reindex=rec.reindex, facts=rec.facts_for, limit=1)


def test_a_label_the_policy_agrees_with_is_not_drift(client, chunk_index, db_session, normal_user):
    """Control: voiceprints on, the index holds the voice-made name the resolver derives."""
    set_instance_switch(db_session, "voiceprints", True)
    media_file, _ = _voice_named_file(db_session, normal_user)
    _index_chunk(client, chunk_index, media_file, VOICE_NAME)
    rec = _Recorder()

    outcome = _scan_one(db_session, media_file, rec)

    assert (outcome.scanned, outcome.drifted) == (1, 0)
    assert rec.reindexed == []


def test_turning_voiceprints_off_leaves_drift_the_scan_finds_in_the_real_index(
    client, chunk_index, db_session, normal_user
):
    """The index still names the person after the switch moved: the stale name is searchable."""
    media_file, _ = _voice_named_file(db_session, normal_user)
    _index_chunk(client, chunk_index, media_file, VOICE_NAME)
    set_instance_switch(db_session, "voiceprints", False)
    rec = _Recorder()

    outcome = _scan_one(db_session, media_file, rec)

    assert (outcome.scanned, outcome.drifted, outcome.queued) == (1, 1, 1)
    assert rec.reindexed == [media_file.id]
    assert rec.facts == [media_file.id], "the facts roster fingerprints the label too"


def test_after_the_relabel_lands_the_drift_scan_is_clean_again(
    client, chunk_index, db_session, normal_user
):
    """Resync end to end against the real index: request, run, rewrite the label, re-scan."""
    set_instance_switch(db_session, "voiceprints", True)
    media_file, _ = _voice_named_file(db_session, normal_user)
    _index_chunk(client, chunk_index, media_file, VOICE_NAME)
    set_instance_switch(db_session, "voiceprints", False)
    rec = _Recorder()

    label_reindex.request_label_resync(
        db_session,
        ResyncScope("instance"),
        actor_user_id=None,
        switch="voiceprints",
        from_value=True,
        to_value=False,
    )
    outcomes = label_reindex.run_label_resync(db_session, reindex=rec.reindex, facts=rec.facts_for)
    assert media_file.id in rec.reindexed, "the resync did not select the voice-named file"
    assert all(o.finished for o in outcomes)
    assert label_reindex.read_pending(db_session) == [], (
        "a finished scope leaves the pending record"
    )

    assert _scan_one(db_session, media_file, _Recorder()).drifted == 1  # not yet relabelled
    _index_chunk(client, chunk_index, media_file, "SPEAKER_00")  # what the re-index writes
    assert _scan_one(db_session, media_file, _Recorder()).drifted == 0


# --- purge: documents across every speaker index, unstamped ones included ------------------


def test_a_user_purge_removes_the_scopes_documents_from_every_index_and_only_those(
    client, speaker_indices, db_session, normal_user
):
    idx = speaker_indices
    mine, other_speaker = str(uuid.uuid4()), str(uuid.uuid4())
    profile, cluster = str(uuid.uuid4()), str(uuid.uuid4())
    # Unstamped (predates the tenant backfill): no organization_id, and in BOTH versions.
    _put(client, idx["v3"], mine, user_id=normal_user.id)
    _put(client, idx["v4"], mine, user_id=normal_user.id)
    _put(client, idx["v4"], f"profile_{profile}", user_id=normal_user.id, document_type="profile")
    _put(
        client, idx["backup"], f"cluster_{cluster}", user_id=normal_user.id, document_type="cluster"
    )
    _put(client, idx["v4"], other_speaker, user_id=normal_user.id + 1)
    targets = VoiceprintTargets(
        speaker_uuids=[mine], profile_uuids=[profile], cluster_uuids=[cluster]
    )
    ids = [mine, f"profile_{profile}", f"cluster_{cluster}"]
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids

    assert count_speaker_docs_by_ids(ids) == 4, "control: the documents exist before the purge"

    errors: list[dict[str, Any]] = []
    deleted = purge_voiceprint_docs(PurgeScope("user", user_id=normal_user.id), targets, errors)

    assert errors == []
    assert deleted == {
        "speaker_docs_deleted": 2,  # v3 and v4: the alias over v4 is not a third copy
        "profile_docs_deleted": 1,
        "cluster_docs_deleted": 1,  # only the legacy backup held it
    }
    assert count_speaker_docs_by_ids(ids) == 0, "residual must be 0 across v3, v4, alias and backup"
    assert _present(client, idx, other_speaker) == 1, "a document outside the scope was deleted"


def test_an_organization_purge_reaches_unstamped_and_orphan_documents_but_not_another_orgs(
    client, speaker_indices, db_session, normal_user
):
    idx = speaker_indices
    org, other_org = 910_001, 910_002
    unstamped_row, stamped_orphan, other = (str(uuid.uuid4()) for _ in range(3))
    _put(client, idx["v4"], unstamped_row, user_id=normal_user.id)  # Postgres row, no stamp
    _put(client, idx["v4"], stamped_orphan, user_id=normal_user.id, organization_id=org)  # no row
    _put(client, idx["v4"], other, user_id=normal_user.id, organization_id=other_org)
    scope = PurgeScope("organization", organization_id=org)
    assert org_stamp_clause(scope, "speaker") is not None
    targets = VoiceprintTargets(speaker_uuids=[unstamped_row])

    errors: list[dict[str, Any]] = []
    deleted = purge_voiceprint_docs(scope, targets, errors)

    assert errors == []
    assert deleted["speaker_docs_deleted"] == 2
    assert _present(client, idx, unstamped_row) == 0, "an UNSTAMPED document was missed"
    assert _present(client, idx, stamped_orphan) == 0
    assert _present(client, idx, other) == 1, "another organization's document was deleted"


def test_a_stamp_only_delete_would_have_missed_the_unstamped_document(
    client, speaker_indices, normal_user
):
    """Why the purge selects by id: the organization's own stamp does not reach it."""
    idx = speaker_indices
    unstamped = str(uuid.uuid4())
    _put(client, idx["v4"], unstamped, user_id=normal_user.id)
    clause = org_stamp_clause(PurgeScope("organization", organization_id=910_001), "speaker")

    response = client.count(index=idx["v4"], body={"query": clause})

    assert response["count"] == 0
    assert _present(client, idx, unstamped) == 1
