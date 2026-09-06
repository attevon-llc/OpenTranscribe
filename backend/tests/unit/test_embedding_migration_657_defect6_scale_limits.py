"""Three silent scale limits + a wrong ETA in the v4 migration (issue #657, defect 6).

1. ``_get_already_migrated_file_ids`` used a ``terms`` aggregation capped at
   50,000 buckets, which UNDER-reports past that many distinct
   ``media_file_id`` values rather than erroring — a composite aggregation
   pages through all of them.
2. The presigned URL for migration source audio was a flat 2h, unrelated to
   the deployment's actual signing-credential ceiling
   (``PRESIGNED_URL_MAX_SECONDS`` / ``clamp_presigned_expiry``).
3. The Redis progress TTL is now a named constant (``MIGRATION_STATUS_TTL_SECONDS``,
   72h) instead of a bare ``86400`` repeated in two places (the initial SET
   and the Lua increment script), so both stay in sync by construction.
4. The UI ETA multiplied speaker DOCUMENT count by a per-file constant; the
   status endpoint now also reports ``completed_file_count``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import migration_progress_service as progress_mod
from app.tasks import embedding_migration_v4 as mig
from app.tasks import migration_pipeline as pipeline_mod


@pytest.mark.unit
class TestAlreadyMigratedFileIdsPaginatesPastTermsCap:
    def test_composite_aggregation_pages_through_two_batches(self, monkeypatch):
        """Simulate more distinct media_file_id values than a single composite
        page (page_size below is patched small to keep the test fast) —
        proving pagination happens rather than truncating at one response.
        """
        client = MagicMock()
        client.indices.exists.return_value = True

        # Page 1: 2 buckets + after_key. Page 2: 1 bucket, no more after (fewer
        # than page_size -> loop stops).
        responses = [
            {
                "aggregations": {
                    "file_ids": {
                        "buckets": [
                            {"key": {"media_file_id": 1}, "doc_count": 2},
                            {"key": {"media_file_id": 2}, "doc_count": 3},
                        ],
                        "after_key": {"media_file_id": 2},
                    }
                }
            },
            {
                "aggregations": {
                    "file_ids": {
                        "buckets": [{"key": {"media_file_id": 3}, "doc_count": 1}],
                        "after_key": {"media_file_id": 3},
                    }
                }
            },
        ]
        client.search.side_effect = responses

        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)
        # embeddable == v4 doc_count for every file -> "fully migrated".
        embeddable_by_id = {1: 2, 2: 3, 3: 1}
        monkeypatch.setattr(
            mig,
            "_count_embeddable_speakers_per_file",
            lambda ids: {i: embeddable_by_id[i] for i in ids},
        )
        monkeypatch.setattr(mig, "get_speaker_index_v4", lambda: "speakers_v4")
        monkeypatch.setattr(mig, "_ALREADY_MIGRATED_COMPOSITE_PAGE_SIZE", 2)

        result = mig._get_already_migrated_file_ids()

        assert client.search.call_count == 2
        assert result == {1, 2, 3}

    def test_no_hardcoded_50000_terms_cap_remains(self):
        """A plain string check that the old under-reporting shape (a `terms`
        agg with `"size": 50000`) is gone from the function's source."""
        import inspect

        source = inspect.getsource(mig._get_already_migrated_file_ids)
        assert "50000" not in source
        assert "composite" in source


@pytest.mark.unit
class TestMigrationPresignedUrlUsesDeploymentCeiling:
    def test_presigned_expiry_no_longer_hardcoded_two_hours(self):
        import inspect

        source = inspect.getsource(pipeline_mod.prepare_file)
        assert "hours=2" not in source
        assert "max_presigned_seconds" in source


@pytest.mark.unit
class TestMigrationStatusTtlIsANamedConstantUsedEverywhere:
    def test_ttl_constant_used_in_start_and_increment(self):
        assert progress_mod.MIGRATION_STATUS_TTL_SECONDS > 24 * 60 * 60
        # The Lua script takes the TTL as ARGV[4] rather than a bare literal,
        # so it can never drift from the constant used by start_migration().
        lua = progress_mod.MigrationProgressService._INCREMENT_LUA
        assert '"EX", ARGV[4]' in lua
        assert "86400" not in lua
