"""``ingest_artifacts/scope.py`` — the mixed recording+document coverage map.

The #403 Stage-6 gate: a collection of recordings AND documents must produce a
``file_facts`` map covering every member, counting (never dropping) any member with no
artifacts yet. This is the shape ``chat/mapreduce.scope_digest_hits`` — outside this
lane's file set — needs to grow into; exercised directly here against real Postgres so
the coverage semantics are proven independent of that integration.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.document import Document
from app.models.file_facts import FileFacts
from app.models.media import MediaFile
from app.models.user import User
from app.services.ingest_artifacts.scope import scope_facts_for_uuids


def _new_user(db) -> User:
    user = User(
        email=f"scope_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db.add(user)
    db.flush()
    return user


def _new_media_file(db, user: User, *, with_facts: bool) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="recording.wav",
        storage_path="x/recording.wav",
        file_size=1,
        content_type="audio/wav",
        title="A recording",
    )
    db.add(media_file)
    db.flush()
    if with_facts:
        db.add(
            FileFacts(
                media_file_id=media_file.id,
                generator_version="1.1.1",
                source_fingerprint="a" * 64,
                facts={"roster": ["Dana"]},
                digest={"sections": [{"index": 0, "text": "recording digest"}]},
                keyphrases={"phrases": []},
            )
        )
        db.flush()
    return media_file


def _new_document(db, user: User, *, with_facts: bool) -> Document:
    document = Document(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="report.pdf",
        storage_path="x/report.pdf",
        file_size=1,
        content_type="application/pdf",
    )
    db.add(document)
    db.flush()
    if with_facts:
        db.add(
            FileFacts(
                document_id=document.id,
                generator_version="1.1.1",
                source_fingerprint="b" * 64,
                facts={"word_count": 500},
                digest={"sections": [{"index": 0, "text": "document digest"}]},
                keyphrases={"phrases": []},
            )
        )
        db.flush()
    return document


@pytest.mark.xdist_group("ingest_artifacts_scope")
def test_mixed_scope_covers_recordings_and_documents_with_file_facts(db_session):
    user = _new_user(db_session)
    media_file = _new_media_file(db_session, user, with_facts=True)
    document = _new_document(db_session, user, with_facts=True)

    coverage = scope_facts_for_uuids(db_session, [str(media_file.uuid), str(document.uuid)])

    assert coverage.files_total == 2
    assert coverage.files_without_artifacts == 0
    assert {hit.kind for hit in coverage.hits} == {"media", "document"}
    kinds = {hit.uuid: hit.kind for hit in coverage.hits}
    assert kinds[str(media_file.uuid)] == "media"
    assert kinds[str(document.uuid)] == "document"


@pytest.mark.xdist_group("ingest_artifacts_scope")
def test_mixed_scope_counts_members_without_file_facts_rather_than_dropping_them(db_session):
    user = _new_user(db_session)
    media_with_facts = _new_media_file(db_session, user, with_facts=True)
    media_without_facts = _new_media_file(db_session, user, with_facts=False)
    document_with_facts = _new_document(db_session, user, with_facts=True)
    document_without_facts = _new_document(db_session, user, with_facts=False)

    coverage = scope_facts_for_uuids(
        db_session,
        [
            str(media_with_facts.uuid),
            str(media_without_facts.uuid),
            str(document_with_facts.uuid),
            str(document_without_facts.uuid),
        ],
    )

    assert coverage.files_total == 4
    # Coverage is not ranking: every scope member is accounted for, either as a hit
    # or as a counted gap — never silently absent.
    assert coverage.files_without_artifacts == 2
    assert len(coverage.hits) == 2
    hit_uuids = {hit.uuid for hit in coverage.hits}
    assert hit_uuids == {str(media_with_facts.uuid), str(document_with_facts.uuid)}


@pytest.mark.xdist_group("ingest_artifacts_scope")
def test_mixed_scope_counts_a_uuid_matching_neither_table(db_session):
    """A uuid that resolves to nothing (deleted since scope resolution, or invalid) is
    still counted rather than silently vanishing from the coverage total.
    """
    coverage = scope_facts_for_uuids(db_session, [str(uuid_pkg.uuid4())])
    assert coverage.files_total == 1
    assert coverage.files_without_artifacts == 1
    assert coverage.hits == []


def test_scope_facts_for_uuids_empty_scope_is_a_no_op():
    coverage = scope_facts_for_uuids(db=None, file_uuids=[])
    assert coverage.files_total == 0
    assert coverage.files_without_artifacts == 0
    assert coverage.hits == []
