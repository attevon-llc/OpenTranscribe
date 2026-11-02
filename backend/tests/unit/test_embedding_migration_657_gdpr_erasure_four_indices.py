"""GDPR Art. 17 voiceprint erasure must still cover all four indices (issue #657).

The task explicitly calls out ``gdpr_voiceprint_erasure.py``'s erasure
(delete-by-query over ``{alias, v3, v4, v3_backup}``, the set
``speaker_maintenance.speaker_embedding_indices`` names) as a location that
must not regress: its own docstring records that
``_restore_v3_from_backup`` once RESURRECTED erased biometric data. None of
the six #657 defect fixes touch this function, but it is exactly the kind of
site an unrelated refactor could silently narrow (e.g. by "simplifying" the
index set while fixing the alias-resolver disagreement in defect 2) — so this
pins the four-index set as a regression guard rather than relying on nobody
touching it by accident. The guard is behavioural: a recording client must see
the delete (and the residual count) reach all four indices.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.core.embedding_models import get_speaker_index
from app.core.embedding_models import get_speaker_index_v3
from app.core.embedding_models import get_speaker_index_v3_backup
from app.core.embedding_models import get_speaker_index_v4
from app.services import gdpr_voiceprint_erasure
from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

FOUR_INDICES = {
    get_speaker_index(),
    get_speaker_index_v3(),
    get_speaker_index_v4(),
    get_speaker_index_v3_backup(),
}


class _Indices:
    def exists(self, index: str) -> bool:
        return True

    def refresh(self, index: str) -> None:
        return None


class _RecordingClient:
    """Every index exists; records where deletes and counts were sent."""

    def __init__(self) -> None:
        self.indices = _Indices()
        self.deleted_from: list[str] = []
        self.counted_in: list[str] = []

    def delete_by_query(self, index: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.deleted_from.append(index)
        return {"deleted": 1}

    def count(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.counted_in.append(index)
        return {"count": 0}


@pytest.fixture
def client(monkeypatch) -> _RecordingClient:
    from app.services.opensearch_service import client as os_client

    recorder = _RecordingClient()
    monkeypatch.setattr(os_client, "opensearch_client", recorder)
    return recorder


@pytest.mark.unit
class TestVoiceprintErasureCoversAllFourIndices:
    def test_the_speaker_index_set_names_alias_v3_v4_and_v3_backup(self):
        assert len(FOUR_INDICES) == 4
        assert set(speaker_embedding_indices()) == FOUR_INDICES

    def test_erasure_deletes_from_alias_v3_v4_and_v3_backup(self, client):
        errors: list[dict[str, Any]] = []

        gdpr_voiceprint_erasure.delete_speaker_docs_by_query(
            {"query": {"term": {"user_id": 7}}}, errors=errors, user_id=7
        )

        assert errors == []
        assert sorted(client.deleted_from) == sorted(FOUR_INDICES)

    def test_erasure_by_id_deletes_from_every_index(self, client):
        gdpr_voiceprint_erasure.delete_speaker_docs_by_ids(["speaker-uuid"])

        assert sorted(client.deleted_from) == sorted(FOUR_INDICES)

    def test_residual_count_reads_every_index(self, client):
        assert gdpr_voiceprint_erasure.count_speaker_docs_by_ids(["speaker-uuid"]) == 0

        assert client.counted_in
        for target in client.counted_in:
            assert set(target.split(",")) == FOUR_INDICES

    def test_docstring_still_warns_about_backup_resurrection(self):
        """The docstring recording the _restore_v3_from_backup incident is the
        institutional memory for why v3_backup is in the erasure set at all -
        pin its presence so a rewrite cannot silently drop the warning along
        with the behavior it explains.
        """
        source = inspect.getsource(gdpr_voiceprint_erasure)
        assert "speakers_v3_backup" in source
        assert "resurrect" in source.lower() or "restore_v3_from_backup" in source
