"""Erasure tests that pin the SQL plane must not reach the default (retrying) storage client.

Against an unreachable MinIO each erasure that runs ``erase_extras`` spends ~18 s in retry
backoff, and the receipt measurers another ~36 s (issue #1368).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.user import User
from app.services import gdpr_erasure_service
from tests import test_chat_gdpr_erasure
from tests import test_gdpr_ledger_before_destroy

_REQUIRED = {"no_receipt_measurement", "no_erasure_extras"}


def _marked_fixtures(module_or_test) -> set[str]:
    marks = getattr(module_or_test, "pytestmark", [])
    marks = marks if isinstance(marks, list) else [marks]
    return {a for m in marks if m.name == "usefixtures" for a in m.args}


@pytest.mark.parametrize(
    ("module", "test_name"),
    [
        (test_chat_gdpr_erasure, None),
        (
            test_gdpr_ledger_before_destroy,
            "test_the_ledger_entry_is_pending_when_destruction_begins",
        ),
    ],
)
def test_slow_erasure_modules_stub_the_storage_stages(module, test_name):
    target = module if test_name is None else getattr(module, test_name)
    assert _marked_fixtures(target) >= _REQUIRED


@pytest.mark.usefixtures("no_receipt_measurement", "no_erasure_extras")
def test_the_fixtures_keep_erase_user_off_the_storage_client(db_session):
    user = User(email="storage-guard@example.com", hashed_password="x", full_name="g")
    db_session.add(user)
    db_session.flush()
    with patch("app.services.storage_versions.versioning_state") as storage:
        gdpr_erasure_service.erase_user(db_session, int(user.id))
    assert storage.call_count == 0
    assert db_session.query(User).filter(User.email == "storage-guard@example.com").count() == 0
