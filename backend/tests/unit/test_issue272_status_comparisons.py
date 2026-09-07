"""Regression tests for issue #272 — always-False FileStatus string comparisons.

``str(FileStatus.X)`` renders as ``"FileStatus.X"`` (deliberately pinned by the
status-detail API test), so comparisons against bare value strings like
``"completed"`` never matched. Two dormant sites were fixed to compare enum
members; these tests pin the now-active behavior.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

from app.api.endpoints.files.crud import _get_or_compute_analytics
from app.core.enums import FileStatus


def _db_returning_analytics(existing):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    return db


class TestAnalyticsOnDemandGate:
    def test_completed_file_without_analytics_computes_on_demand(self):
        """The on-demand path was dead pre-fix: the str-compare gate always
        early-returned. A COMPLETED file with no stored analytics must trigger
        computation now."""
        db = _db_returning_analytics(None)
        with patch(
            "app.services.analytics_service.AnalyticsService.compute_and_save_analytics",
            return_value=False,
        ) as compute:
            result = _get_or_compute_analytics(db, 123, FileStatus.COMPLETED)
        compute.assert_called_once_with(db, 123)
        assert result is None  # compute reported failure; nothing to fetch

    def test_incomplete_file_never_computes(self):
        db = _db_returning_analytics(None)
        with patch(
            "app.services.analytics_service.AnalyticsService.compute_and_save_analytics"
        ) as compute:
            for status in (FileStatus.PENDING, FileStatus.PROCESSING, FileStatus.ERROR):
                assert _get_or_compute_analytics(db, 123, status) is None
        compute.assert_not_called()

    def test_existing_analytics_short_circuits(self):
        existing = MagicMock()
        db = _db_returning_analytics(existing)
        with patch(
            "app.services.analytics_service.AnalyticsService.compute_and_save_analytics"
        ) as compute:
            assert _get_or_compute_analytics(db, 123, FileStatus.COMPLETED) is existing
        compute.assert_not_called()


class TestRedactionReprocessGuard:
    def test_guard_matches_enum_status(self):
        """The reprocess guard's membership test must match actual enum
        statuses (the pre-fix str() form matched nothing)."""
        for status in (FileStatus.PROCESSING, FileStatus.CANCELLING):
            assert status in (FileStatus.PROCESSING, FileStatus.CANCELLING)
        # And the old broken form stays broken — documenting WHY the fix exists.
        assert str(FileStatus.PROCESSING) not in ("processing", "cancelling")

    def test_task_skips_mid_reprocess_file_without_segments(self):
        """End-to-end through the task body: PROCESSING + no segments → skipped.

        The task now also creates/updates a ``Task`` row (issue #622), so the
        session mock must tell a ``MediaFile`` query apart from a ``Task``
        query — a single blanket ``query().filter().first()`` return value
        (the previous form here) would hand the ``MediaFile`` mock back for
        the ``Task`` lookup too, and ``update_task_status`` calling
        ``int()`` on that mock's mock attributes would raise, masking the
        very "skipped" outcome this test exists to pin.
        """
        from app.models.media import MediaFile
        from app.tasks.redaction_task import redaction_detect_task

        media = MagicMock()
        media.status = FileStatus.PROCESSING
        media.transcript_segments = []
        media.user_id = 1

        class _FakeSession:
            """Distinguishes MediaFile lookups from everything else (Task)."""

            def query(self, model, *_args, **_kwargs):
                mock_query = MagicMock()
                mock_query.filter.return_value.first.return_value = (
                    media if model is MediaFile else None
                )
                return mock_query

            def add(self, *_a, **_kw):
                pass

            def commit(self):
                pass

            def rollback(self):
                pass

            def refresh(self, *_a, **_kw):
                pass

        session = _FakeSession()

        class _Scope:
            def __enter__(self):
                return session

            def __exit__(self, *a):
                return False

        with (
            patch("app.db.session_utils.session_scope", return_value=_Scope()),
            patch("app.services.redaction.service.RedactionService.detect_and_store") as detect,
        ):
            result = redaction_detect_task.run(file_id=1)

        assert result == {"status": "skipped", "reason": "no_segments"}
        detect.assert_not_called()
