"""Display labels travel with a stable code so the SPA can translate them.

The English ``label`` / ``description`` stays as the fallback for a client that does not know a
code; these tests pin the code itself, which is what the frontend keys its strings on.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.tasks import opensearch_integrity_task as integrity


def test_index_overview_entries_carry_a_kind_code():
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.return_value = {"count": 3}
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch(
            "app.db.session_utils.session_scope",
            side_effect=OperationalError("select", {}, Exception("no db")),
        ),
    ):
        overview = integrity.get_index_overview()

    kinds = [entry["kind"] for entry in overview["indices"]]
    assert kinds == ["speakers_v3", "speakers_v4", "transcripts", "summaries"]
    assert all(entry["label"] for entry in overview["indices"])


def test_active_local_model_info_carries_the_vram_numbers():
    from app.api.endpoints import asr_local_model

    with (
        patch.object(asr_local_model, "find_system_setting", return_value=None),
        patch.object(asr_local_model.settings, "WHISPER_MODEL", "large-v3-turbo"),
        patch.object(asr_local_model.worker_report, "read_loaded_models", return_value=[]),
        patch("app.services.asr.model_discovery.discover_local_models", return_value=[]),
    ):
        body = asr_local_model.get_active_local_model(db=MagicMock(), current_user=MagicMock())

    info = body["model_info"]
    assert info["vram_hint_gb"] == 6
    assert info["is_default"] is True
    assert info["description"]  # the English sentence stays as the fallback
