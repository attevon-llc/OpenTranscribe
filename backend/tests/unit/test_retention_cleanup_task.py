"""``cleanup_expired_files`` — the hourly task that DELETES user media, previously untested.

Three defects made a broken retention job indistinguishable from a working one:
a catch-all that returned ``{"status": "error"}`` (Celery records that as SUCCESS),
a ``run_time`` parse that raised straight into it, and a forced run that claimed
the day's slot and suppressed the scheduled pass.

Every test drives the task FUNCTION BODY (``.run``) against the savepoint-rolled-back
``db_session``, with a 100-year retention window so no real file is ever eligible.
"""

import contextlib
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.models.document import Document
from app.services import system_settings_service
from app.tasks import cleanup

pytestmark = pytest.mark.xdist_group("retention_system_settings")

#: A retention window nothing in any dev database can fall outside of.
_UNREACHABLE_RETENTION_DAYS = 36500


#: 02:30 UTC — inside the default 02:00 retention hour, so the scheduled-hour
#: guard is exercised deterministically instead of only passing between 02:00
#: and 03:00 UTC.
_FIXED_NOW = datetime(2026, 8, 12, 2, 30, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is :data:`_FIXED_NOW`, in any timezone."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - inherited contract
        return _FIXED_NOW.astimezone(tz) if tz is not None else _FIXED_NOW


def _config(**overrides):
    """A retention config dict shaped like ``get_retention_config``'s return."""
    config = {
        "retention_enabled": True,
        "retention_days": _UNREACHABLE_RETENTION_DAYS,
        "delete_error_files": False,
        "run_time": "02:00",
        "timezone": "UTC",
        "last_run": None,
        "last_run_deleted": 0,
    }
    config.update(overrides)
    return config


@pytest.fixture
def retention_env(db_session, monkeypatch):
    """Point the task's session and config reader at the test transaction.

    Returns:
        A callable taking the config overrides the task should read.
    """
    monkeypatch.setattr(
        cleanup, "session_scope", lambda: contextlib.nullcontext(db_session), raising=True
    )
    monkeypatch.setattr(cleanup, "datetime", _FrozenDatetime, raising=True)

    def _configure(**overrides):
        config = _config(**overrides)
        monkeypatch.setattr(
            system_settings_service, "get_retention_config", lambda db: config, raising=True
        )
        return config

    return _configure


def test_unexpected_failure_is_reported_as_a_task_failure(retention_env, monkeypatch):
    """Defect: ``except Exception -> {"status": "error"}``, which Celery records as SUCCESS.

    A retention job broken by an unreadable settings row or an unreachable MinIO
    looked healthy on every one of its hourly runs. The exception must propagate.
    """
    retention_env()

    def _explode(db):
        raise RuntimeError("settings table is unreadable")

    monkeypatch.setattr(system_settings_service, "get_retention_config", _explode, raising=True)

    with pytest.raises(RuntimeError, match="settings table is unreadable"):
        cleanup.cleanup_expired_files.run(force=True)


def test_malformed_run_time_parses_to_the_documented_default():
    """Defect: ``int(config["run_time"].split(":")[0])`` raised on any malformed value.

    That exception landed in the catch-all above, so a single bad settings row
    stopped retention permanently while reporting success. An unparseable schedule
    now falls back to the coded default hour.
    """
    assert cleanup._scheduled_retention_hour("not-a-time") == cleanup._DEFAULT_RETENTION_HOUR
    assert cleanup._scheduled_retention_hour(None) == cleanup._DEFAULT_RETENTION_HOUR
    assert cleanup._scheduled_retention_hour("99:00") == cleanup._DEFAULT_RETENTION_HOUR
    assert cleanup._scheduled_retention_hour("05:30") == 5


def test_malformed_run_time_still_lets_the_pass_run(retention_env):
    """Defect (whole-task view): a malformed ``run_time`` aborted the run entirely.

    With the fallback hour reached, the pass completes normally instead of
    returning an error status that nothing was watching.
    """
    retention_env(run_time="garbage")

    result = cleanup.cleanup_expired_files.run()

    assert result["status"] == "completed"
    assert result["deleted"] == 0


def test_forced_run_does_not_claim_the_scheduled_slot(retention_env, monkeypatch):
    """Defect: a forced run stamped ``files.retention_last_run``.

    That field is exactly what the already-ran-today guard reads, so an admin
    pressing "run now" cancelled the day's scheduled pass — the one that honours
    the configured window and error-file policy.
    """
    retention_env()
    written: list[str] = []
    monkeypatch.setattr(
        system_settings_service,
        "set_setting",
        lambda db, key, value, *args, **kwargs: written.append(key),
        raising=True,
    )

    result = cleanup.cleanup_expired_files.run(force=True)

    assert result["status"] == "completed"
    assert written == ["files.retention_last_run_deleted"]


def test_scheduled_run_does_claim_the_slot(retention_env, monkeypatch):
    """Control: the scheduled pass must still record itself, or it repeats hourly.

    Same code path as the test above with ``force=False``: the stamp is written,
    proving the exclusion is conditional rather than a removed feature.
    """
    retention_env(run_time="02:00")
    written: list[str] = []
    monkeypatch.setattr(
        system_settings_service,
        "set_setting",
        lambda db, key, value, *args, **kwargs: written.append(key),
        raising=True,
    )

    result = cleanup.cleanup_expired_files.run()

    assert result["status"] == "completed"
    assert written == ["files.retention_last_run", "files.retention_last_run_deleted"]


def test_retention_disabled_short_circuits(retention_env):
    """Control: the enabled flag still wins over everything else.

    Guards the guard order — a refactor that moved the run_time parse ahead of
    the enabled check would run retention on a deployment that switched it off.
    """
    retention_env(retention_enabled=False, run_time="garbage")

    result = cleanup.cleanup_expired_files.run()

    assert result["status"] == "disabled"


# --------------------------------------------------------------------------- #
# Documents join retention (v399, #362 lane C4) — previously this table was     #
# never queried by this task, so a document never expired at all.              #
# --------------------------------------------------------------------------- #
def _mk_document(db, user, *, age_days: int, legal_hold: bool = False) -> Document:
    # Relative to the FROZEN "now" the task itself reads (`_FrozenDatetime`), not real
    # wall-clock time — the retention cutoff cleanup.py computes is frozen, so a
    # document's age must be measured against the same clock or the two can disagree
    # depending on what day this test happens to run.
    old = _FIXED_NOW - timedelta(days=age_days)
    duuid = uuid_pkg.uuid4()
    doc = Document(
        uuid=duuid,
        user_id=user.id,
        filename=f"retain_{duuid.hex[:8]}.pdf",
        storage_path=f"documents/test/{duuid}.pdf",
        content_type="application/pdf",
        file_size=100,
        status="completed",
        legal_hold=legal_hold,
        created_at=old,
        parsed_at=old,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


#: ⚠️ SAFETY: these tests must NEVER call the whole `cleanup_expired_files` task with a
#: short/finite `retention_days`. That sweeps `_select_expired_files`/`_purge_expired_files`
#: over the WHOLE `media_file` table too — not just rows this test created — and on a
#: shared dev database that is real, irreversible data loss: it happened once while this
#: suite was being written (a `retention_days=1` run here permanently deleted the MinIO
#: objects for 16 real media files; the Postgres rows and OpenSearch index were fine
#: because `db_session` rolls the DB back, but `delete_file`/`delete_by_query` are real
#: network calls with no such rollback). `_UNREACHABLE_RETENTION_DAYS` above exists
#: for exactly this reason and every MediaFile-touching test in this file already uses
#: it. The two document-selection/purge functions below are therefore exercised DIRECTLY
#: — never through `cleanup_expired_files.run()` — so they cannot touch `media_file` at
#: all, regardless of what window is configured.
def _resolve_none(_organization_id):
    """Community-edition ``resolve_retention_days`` stand-in: no per-org override."""
    return None


def test_an_expired_document_is_selected_and_purged(db_session, normal_user):
    """Documents past the retention window are found and swept — the gap this guards
    (this table was never queried by this task at all before v399).
    """
    doc = _mk_document(db_session, normal_user, age_days=10)
    doc_id = doc.id
    cutoff = _FIXED_NOW - timedelta(days=1)
    config = _config(delete_error_files=False)

    selected = cleanup._select_expired_documents(db_session, config, cutoff, cutoff, _resolve_none)
    assert (doc_id, str(doc.uuid), doc.storage_path) in selected

    with (
        patch.object(cleanup, "session_scope", lambda: contextlib.nullcontext(db_session)),
        patch(
            "app.services.search.indexing_service.TranscriptIndexingService.delete_transcript_chunks",
            return_value=None,
        ),
        patch("app.services.minio_service.delete_file", return_value=None),
    ):
        deleted, failed = cleanup._purge_expired_documents(selected)

    assert deleted == 1
    assert failed == 0
    assert db_session.query(Document).filter(Document.id == doc_id).first() is None


def test_a_fresh_document_is_not_selected(db_session, normal_user):
    """Control: a document younger than the window is left out of the candidate set."""
    doc = _mk_document(db_session, normal_user, age_days=1)
    cutoff = _FIXED_NOW - timedelta(days=365)
    config = _config(delete_error_files=False)

    selected = cleanup._select_expired_documents(db_session, config, cutoff, cutoff, _resolve_none)

    assert doc.id not in {d[0] for d in selected}


def test_a_legal_hold_document_is_not_selected(db_session, normal_user):
    """A held document is excluded from the candidate set (Art. 17(3)(e) reasoning
    applied here for the same protection ``gdpr_erasure_service`` already gives it).
    """
    doc = _mk_document(db_session, normal_user, age_days=10, legal_hold=True)
    cutoff = _FIXED_NOW - timedelta(days=1)
    config = _config(delete_error_files=False)

    selected = cleanup._select_expired_documents(db_session, config, cutoff, cutoff, _resolve_none)

    assert doc.id not in {d[0] for d in selected}
