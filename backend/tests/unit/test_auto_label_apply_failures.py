"""``auto_apply_suggestions`` reports a database error on one suggestion as a FAILURE.

Below-threshold and blank suggestions are skips: the labeler chose not to apply them. A
database error is not a choice, so it must not be counted among the skips, must not stop
the remaining suggestions, and must leave the suggestion un-stamped so a later
retroactive run picks it up again.

The error is a real one (``SELECT 1/0`` aborts the Postgres transaction), not a raised
stand-in: an aborted transaction is what made every later suggestion fail too.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import text

from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.topic import TopicSuggestion
from app.services.auto_label_service import AutoLabelService


def _u(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _suggestion(gov, media_file, *, tags=(), collections=()) -> TopicSuggestion:
    row = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=media_file.user_id,
        suggested_tags=list(tags),
        suggested_collections=list(collections),
        status="pending",
    )
    gov.db.add(row)
    gov.db.commit()
    gov.db.refresh(row)
    return row


@pytest.fixture()
def failing_name(monkeypatch):
    """Make the tag/collection resolver hit a real database error for one name."""
    names: set[str] = set()
    real_tag = AutoLabelService._get_or_create_tag_with_dedup
    real_coll = AutoLabelService._get_or_create_collection_with_dedup

    def tag(self, name, *args, **kwargs):
        if name in names:
            self.db.execute(text("SELECT 1/0"))
        return real_tag(self, name, *args, **kwargs)

    def coll(self, name, *args, **kwargs):
        if name in names:
            self.db.execute(text("SELECT 1/0"))
        return real_coll(self, name, *args, **kwargs)

    monkeypatch.setattr(AutoLabelService, "_get_or_create_tag_with_dedup", tag)
    monkeypatch.setattr(AutoLabelService, "_get_or_create_collection_with_dedup", coll)
    return names


def test_a_database_error_is_a_failure_not_a_skip(gov, failing_name, caplog):
    f = gov.file(gov.alice, org=None)
    t_first, t_bad, t_after, t_low = _u("First"), _u("Broken"), _u("After"), _u("Low")
    c_bad, c_after = _u("Coll Broken"), _u("Coll After")
    failing_name.update({t_bad, c_bad})
    suggestion = _suggestion(
        gov,
        f,
        tags=[
            {"name": t_first, "confidence": 0.9},
            {"name": t_bad, "confidence": 0.9},
            {"name": t_after, "confidence": 0.9},
            {"name": t_low, "confidence": 0.1},
        ],
        collections=[
            {"name": c_bad, "confidence": 0.9},
            {"name": c_after, "confidence": 0.9},
        ],
    )

    with caplog.at_level(logging.ERROR, logger="app.services.auto_label_service"):
        result = AutoLabelService(gov.db).auto_apply_suggestions(
            f, suggestion, gov.alice.id, confidence_threshold=0.5
        )

    assert result["failed_tags"] == [t_bad]
    assert result["skipped_tags"] == [t_low]
    assert result["auto_applied_tags"] == [t_first, t_after]
    assert result["failed_collections"] == [c_bad]
    assert result["skipped_collections"] == []
    assert result["auto_applied_collections"] == [c_after]

    gov.db.expire_all()
    assert gov.db.query(FileTag).filter_by(media_file_id=f.id).count() == 2
    assert gov.db.query(CollectionMember).filter_by(media_file_id=f.id).count() == 1
    gov.db.refresh(suggestion)
    assert suggestion.auto_apply_completed_at is None
    assert suggestion.auto_applied_tags is not None
    assert sorted(suggestion.auto_applied_tags) == sorted([t_first, t_after])

    failures = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert len(failures) == 2


def test_a_clean_run_has_no_failures_and_is_stamped_complete(gov, failing_name):
    f = gov.file(gov.alice, org=None)
    t_ok, c_ok = _u("Fine Tag"), _u("Fine Coll")
    suggestion = _suggestion(
        gov,
        f,
        tags=[{"name": t_ok, "confidence": 0.9}],
        collections=[{"name": c_ok, "confidence": 0.9}],
    )

    result = AutoLabelService(gov.db).auto_apply_suggestions(
        f, suggestion, gov.alice.id, confidence_threshold=0.5
    )

    assert result["failed_tags"] == []
    assert result["failed_collections"] == []
    assert result["auto_applied_tags"] == [t_ok]
    assert result["auto_applied_collections"] == [c_ok]
    gov.db.refresh(suggestion)
    assert suggestion.auto_apply_completed_at is not None
