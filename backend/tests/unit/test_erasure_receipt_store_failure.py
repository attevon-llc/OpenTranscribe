"""A receipt that could not be stored must not let the erasure close as complete."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.models.erasure import ErasureLedgerEntry
from app.services import erasure_ledger_service as ledger
from app.services.erasure_receipt import flow
from app.services.erasure_receipt.receipt import ReceiptData
from app.services.erasure_receipt.types import ErasureScope

_VERIFIED = ReceiptData(outcome="verified", deleted={"pg.media_file": 1})


def _entry(db_session, user):
    entry = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=user.id,
        subject_user_uuid=user.uuid,
        actor_kind="super_admin",
    )
    assert entry is not None
    return entry


def _finish(db_session, entry, monkeypatch, stored):
    monkeypatch.setattr(flow, "measure_all", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(flow, "build_receipt", lambda *_a, **_k: _VERIFIED)
    monkeypatch.setattr(flow, "persist_receipt", lambda *_a, **_k: stored)
    monkeypatch.setattr(flow, "_schedule_reverify", lambda *_a, **_k: None)
    summary = {"errors": [], "legal_holds_skipped": 0}
    scope = ErasureScope(kind="user", user_id=entry.subject_user_id)
    flow.finish(db_session, entry, scope, MagicMock(), MagicMock(), summary)
    return summary


def test_an_unstored_receipt_leaves_the_entry_open(db_session, normal_user, monkeypatch):
    entry = _entry(db_session, normal_user)
    summary = _finish(db_session, entry, monkeypatch, stored=None)

    assert summary["receipt"]["uuid"] is None
    assert summary["receipt"]["outcome"] == "unverified"
    assert [e["stage"] for e in summary["errors"]] == ["receipt"]
    ledger.record_outcome(db_session, entry, summary)
    row = db_session.get(ErasureLedgerEntry, entry.id)
    assert row is not None
    assert (row.status, row.deferred_reason) == ("deferred", "error")


def test_a_stored_verified_receipt_still_completes(db_session, normal_user, monkeypatch):
    entry = _entry(db_session, normal_user)
    stored = MagicMock()
    stored.uuid = "00000000-0000-0000-0000-000000000001"
    summary = _finish(db_session, entry, monkeypatch, stored=stored)

    assert summary["errors"] == []
    ledger.record_outcome(db_session, entry, summary)
    row = db_session.get(ErasureLedgerEntry, entry.id)
    assert row is not None and row.status == "complete"
