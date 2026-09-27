"""The legacy ``transcripts`` index carries and enforces the tenant (#1027 C).

Its mapping and writer had no ``organization_id``, so its readers could only be
protected by the org-scoped SQL query they are intersected with. It is now tenant-gated
like the ``transcript_chunks`` plane: an org file's document is stamped, a personal one
carries no field, and each reader ANDs ``tenant_scope.org_filter_clauses`` into its query.
``tenant_backfill_task`` stamps documents written before this.

The first tests need no cluster and run in CI. The rest write real documents through
the real writer into a throwaway index (the REAL mapping, uuid-suffixed, deleted in
teardown) and read them back through the real readers. Every reader call is intersected
with a SQL query that is deliberately NOT tenant-gated, so the only thing that can keep
another tenant's file out of the result is the index gate under test.
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg
from types import SimpleNamespace

import pytest

from app.models.media import MediaFile
from app.models.organization import Organization
from app.services.opensearch_service import transcripts as transcripts_module
from app.services.search.tenant_scope import org_filter_clauses

SCOPES = ("org_a", "org_b", "personal")
WORD = "zylofenix"

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"
_needs_cluster = pytest.mark.skipif(
    _OPENSEARCH_ABSENT,
    reason="No OpenSearch reachable (SKIP_OPENSEARCH): these read real documents back.",
)


class _RecordingClient:
    """Captures the bodies the writer and reader send; answers a search with no hits."""

    def __init__(self) -> None:
        self.indexed: list[dict] = []
        self.searched: list[dict] = []

    def index(self, **kwargs):
        self.indexed.append(kwargs["body"])
        return {"result": "created"}

    def search(self, **kwargs):
        self.searched.append(kwargs["body"])
        return {"hits": {"hits": []}}


@pytest.mark.parametrize(("org_id", "stamped"), [(7, True), (None, False)])
def test_the_writer_stamps_an_org_file_and_leaves_a_personal_one_bare(org_id, stamped, monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(transcripts_module._client, "opensearch_client", fake)
    monkeypatch.setattr(transcripts_module, "ensure_indices_exist", lambda: None)

    transcripts_module.index_transcript(
        1, str(uuid_pkg.uuid4()), 1, WORD, ["SPEAKER_00"], "t", organization_id=org_id
    )

    assert len(fake.indexed) == 1
    doc = fake.indexed[0]
    assert ("organization_id" in doc) is stamped
    if stamped:
        assert doc["organization_id"] == org_id


@pytest.mark.parametrize("org_id", [7, None])
def test_the_gallery_reader_sends_the_tenant_gate(org_id, monkeypatch, db_session):
    from app.api.endpoints.files import filtering

    fake = _RecordingClient()
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)

    filtering.apply_transcript_search_filter(
        db_session.query(MediaFile), WORD, organization_id=org_id
    )

    assert len(fake.searched) == 1
    for clause in org_filter_clauses(org_id):
        assert clause in fake.searched[0]["query"]["bool"]["filter"]


# --------------------------------------------------------------------------- #
# Live cluster: real writer -> real index -> real reader                       #
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(db_session, normal_user, monkeypatch):
    """One user's files in org A, org B and personal scope, each transcript naming WORD."""
    from app.core.config import settings
    from app.services.opensearch_service import client as _client
    from app.services.opensearch_service.indices import transcript_index_body

    client = _client.opensearch_client
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    index_name = f"test_transcripts_tenancy_{uuid_pkg.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", index_name)
    client.indices.create(index=index_name, body=transcript_index_body())

    org_a = Organization(name="tenant-a", slug=f"tenant-a-{uuid_pkg.uuid4().hex[:8]}")
    org_b = Organization(name="tenant-b", slug=f"tenant-b-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add_all([org_a, org_b])
    db_session.flush()
    org_ids = {"org_a": org_a.id, "org_b": org_b.id, "personal": None}

    files = {}
    for scope, org_id in org_ids.items():
        file_uuid = uuid_pkg.uuid4()
        media_file = MediaFile(
            uuid=file_uuid,
            filename=f"{scope}.wav",
            storage_path=f"media/test/{file_uuid}.wav",
            content_type="audio/wav",
            file_size=4096,
            status="completed",
            user_id=normal_user.id,
            organization_id=org_id,
        )
        db_session.add(media_file)
        db_session.flush()
        files[scope] = media_file
    db_session.commit()

    try:
        yield SimpleNamespace(
            client=client,
            index=index_name,
            org_ids=org_ids,
            files=files,
            user=normal_user,
            all_file_ids=[f.id for f in files.values()],
        )
    finally:
        client.indices.delete(index=index_name, ignore=[404])


def _write_through_the_writer(world, scopes=SCOPES) -> None:
    for scope in scopes:
        media_file = world.files[scope]
        transcripts_module.index_transcript(
            media_file.id,
            str(media_file.uuid),
            world.user.id,
            f"they discussed the {WORD} budget",
            ["SPEAKER_00"],
            f"{scope} meeting",
            organization_id=world.org_ids[scope],
        )
    world.client.indices.refresh(index=world.index)


def _gallery_hits(db_session, world, scope) -> set[str]:
    from app.api.endpoints.files import filtering

    ungated = db_session.query(MediaFile).filter(MediaFile.id.in_(world.all_file_ids))
    filtered = filtering.apply_transcript_search_filter(
        ungated, WORD, organization_id=world.org_ids[scope]
    )
    return {str(f.uuid) for f in filtered.all()}


@_needs_cluster
@pytest.mark.xdist_group("opensearch_speaker_indices")
@pytest.mark.parametrize("scope", SCOPES)
def test_the_gallery_transcript_search_returns_only_its_tenants_file(scope, db_session, world):
    _write_through_the_writer(world)

    assert _gallery_hits(db_session, world, scope) == {str(world.files[scope].uuid)}


@_needs_cluster
@pytest.mark.xdist_group("opensearch_speaker_indices")
@pytest.mark.parametrize("scope", SCOPES)
def test_find_speaker_across_media_returns_only_its_tenants_file(scope, world, monkeypatch):
    from app.services.opensearch_service import speaker_metadata

    _write_through_the_writer(world)
    speaker_uuid = str(uuid_pkg.uuid4())
    # The speaker plane is not under test: its doc only supplies the name to look up.
    monkeypatch.setattr(world.client, "get", lambda **_: {"_source": {"name": "SPEAKER_00"}})

    results = speaker_metadata.find_speaker_across_media(
        speaker_uuid, world.user.id, organization_id=world.org_ids[scope]
    )

    assert [r["file_uuid"] for r in results] == [str(world.files[scope].uuid)]


@_needs_cluster
@pytest.mark.xdist_group("opensearch_speaker_indices")
def test_the_backfill_makes_a_pre_1027_org_document_visible_to_its_org(
    db_session, world, monkeypatch
):
    """A document written before the stamp existed is invisible to its org's reader until
    ``run_tenant_backfill`` stamps it — and the run does reach the transcripts index."""
    from app.tasks import tenant_backfill_task

    media_file = world.files["org_a"]
    world.client.index(
        index=world.index,
        id=str(media_file.uuid),
        body={
            "file_id": media_file.id,
            "file_uuid": str(media_file.uuid),
            "user_id": world.user.id,
            "content": f"a legacy {WORD} transcript",
        },
        refresh=True,
    )
    assert _gallery_hits(db_session, world, "org_a") == set()

    @contextlib.contextmanager
    def _this_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _this_session)
    summary = tenant_backfill_task.run_tenant_backfill()

    assert summary["transcript_docs"] >= 1
    assert _gallery_hits(db_session, world, "org_a") == {str(media_file.uuid)}
    assert _gallery_hits(db_session, world, "personal") == set()
