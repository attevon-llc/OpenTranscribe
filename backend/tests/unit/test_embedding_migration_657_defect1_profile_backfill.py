"""Profile-voiceprint backfill at v4 finalize (issue #657, defect 1).

Before this fix, ``_embedding_result_writer`` only ever wrote speaker
documents to v4 — no ``document_type: "profile"`` branch existed anywhere in
the migration. A profile not touched by a since-transcribed file during the
migration window would have no v4 counterpart at all, and finalize's alias
swap would silently strand its voiceprint in v3.

``migrate_profile_documents_to_v4`` recomputes every profile's centroid from
its already-migrated v4 speaker documents and writes it forward before the
existing (#736) profile-count guard runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.tasks import embedding_migration_v4 as mig


@pytest.mark.unit
class TestMigrateProfileDocumentsToV4:
    def test_no_v4_speakers_with_profiles_writes_nothing(self):
        client = MagicMock()
        client.search.return_value = {"hits": {"hits": []}}
        written = mig.migrate_profile_documents_to_v4(client, "speakers_v4")
        assert written == 0

    def test_recomputes_and_writes_one_profile_from_two_speakers(self, monkeypatch):
        client = MagicMock()
        client.search.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "profile_id": 7,
                            "profile_uuid": "profile-uuid-7",
                            "user_id": 1,
                            "organization_id": None,
                            "segment_count": 3,
                            "embedding": [1.0, 0.0],
                        }
                    },
                    {
                        "_source": {
                            "profile_id": 7,
                            "profile_uuid": "profile-uuid-7",
                            "user_id": 1,
                            "organization_id": None,
                            "segment_count": 1,
                            "embedding": [0.0, 1.0],
                        }
                    },
                ]
            },
        }
        # Second scroll page is empty -> loop terminates.
        client.scroll.return_value = {"hits": {"hits": []}}

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.all.return_value = []
        monkeypatch.setattr(mig, "session_scope", lambda: _FakeSessionCtx(db_mock))

        captured: dict = {}

        def _fake_store(**kwargs):
            captured.update(kwargs)
            return True

        with patch(
            "app.services.opensearch_service.store_profile_embedding_v4",
            side_effect=_fake_store,
        ):
            written = mig.migrate_profile_documents_to_v4(client, "speakers_v4")

        assert written == 1
        assert captured["profile_uuid"] == "profile-uuid-7"
        assert captured["speaker_count"] == 2
        # Weighted average of [1,0] (weight 3) and [0,1] (weight 1), L2-normalized.
        assert captured["embedding"][0] > captured["embedding"][1]


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *exc):
        return False


@pytest.mark.unit
class TestFinalizeCallsProfileBackfillBeforeGuard:
    def test_backfill_invoked_before_profile_count_check(self):
        """Red-then-green marker: before this fix, finalize never called any
        profile-backfill function at all, so a profile touched by zero
        migrated files could pass the #736 guard purely by v3==v4==0 and
        would then be silently orphaned by the alias swap. This test proves
        the backfill runs on the finalize path prior to the guard.
        """
        client = MagicMock()
        client.indices.exists.return_value = True
        client.count.return_value = {"count": 10}

        backfill_calls = []

        def _fake_backfill(client_arg, v4_index):
            backfill_calls.append(v4_index)
            return 0

        opensearch_svc_attrs: dict[str, Any] = {
            "get_opensearch_client": lambda: client,
            "get_active_versioned_index": lambda: "speakers_v3",
            "swap_speaker_alias": lambda target: {
                "status": "success",
                "old_target": "speakers_v3",
                "new_target": target,
            },
        }
        mig_attrs: dict[str, Any] = {
            "get_speaker_index_v4": lambda: "speakers_v4",
            "get_speaker_index": lambda: "speakers",
            "migrate_profile_documents_to_v4": _fake_backfill,
            "_count_profile_docs": lambda client_arg, index: 0,
            "send_ws_event": lambda *a, **k: None,
        }
        with (
            patch.multiple("app.services.opensearch_service", **opensearch_svc_attrs),
            patch.multiple(mig, **mig_attrs),
            patch.object(mig.EmbeddingModeService, "clear_cache"),
            patch.object(mig.migration_progress, "clear_status"),
        ):
            result = mig.finalize_v4_migration_task.run(user_id=1)

        assert backfill_calls == ["speakers_v4"]
        assert result["status"] == "success"
