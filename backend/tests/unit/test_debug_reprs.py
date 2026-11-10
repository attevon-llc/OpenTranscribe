"""``__repr__`` of the debug-only value types names what an operator needs to see."""

from __future__ import annotations

from app.core.tenancy import UNSCOPED
from app.models.erasure import ErasureLedgerEntry


def test_unscoped_sentinel_reprs_by_name():
    assert repr(UNSCOPED) == "UNSCOPED"


def test_erasure_ledger_entry_repr_carries_identity_status_and_attempts():
    entry = ErasureLedgerEntry(uuid="abc", subject_type="user", status="failed", attempts=3)

    assert repr(entry) == "<ErasureLedgerEntry abc user status=failed attempts=3>"
