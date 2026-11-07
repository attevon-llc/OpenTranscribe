"""The orphan sweep keeps a trashed file's index documents (#1101).

``opensearch_integrity_task`` deletes documents whose file has no Postgres row. A trashed file
HAS a row until purge, and its documents are what a restore brings back, so they are not
orphans. The sweep answers that from row existence, and must keep doing so: routing its
"valid file" sets through the availability rule (which hides the trash) would make every
trashed file's documents look orphaned and delete them, so a restored file would come back
unsearchable. The live twin runs the sweep against a real index
(``tests/integration/test_trash_index_lifecycle_opensearch.py``).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.models.media import MediaFile
from app.tasks import opensearch_integrity_task as integrity


@pytest.fixture
def trashed_file(monkeypatch, db_session, normal_user) -> MediaFile:
    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=normal_user.id,
        filename=f"integrity_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
        deleted_at=datetime.now(UTC),
        purge_after=datetime.now(UTC) + timedelta(days=30),
        trash_origin="user",
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def test_the_valid_sets_include_a_trashed_file(trashed_file):
    assert trashed_file.id in integrity._get_all_file_ids_from_db()
    assert str(trashed_file.uuid) in integrity._get_all_file_uuids_from_db()


def test_the_recheck_counts_a_trashed_file_as_present(trashed_file):
    assert integrity._file_uuids_present([str(trashed_file.uuid)]) == {str(trashed_file.uuid)}
    assert integrity._file_ids_present([trashed_file.id]) == {str(trashed_file.id)}
