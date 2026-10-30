"""Every GDPR erasure scope opens its ledger entry BEFORE it destroys anything (issue #442).

``erase_user``'s docstring: "The ledger row is committed before anything is destroyed. A
worker that dies part-way leaves a ``pending`` entry the sweep picks up; recording only on
the way out would leave the crash, the case that most needs a record, with none." No test
pinned that order: moving ``_purge_files`` above ``ledger.record_request`` failed nothing in
the throwaway-database run, because every test only inspects the ledger after the erasure
returns, when the entry exists either way.

The first destroy step of every scope is ``_purge_files``. It is replaced here (through
``gdpr_erasure_service``'s globals, where the ``erase_*`` functions resolve it) by a probe
that reads the ledger at the moment destruction would begin.
"""

import uuid as uuid_pkg
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User


@pytest.fixture()
def subject(db_session) -> tuple[User, Organization]:
    from app.core.security import get_password_hash

    user = User(
        email=f"ledger_first_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="ledger subject",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    org = Organization(
        external_org_id=f"org_ledger_{uuid_pkg.uuid4().hex[:8]}", name="Ledger Org", is_active=True
    )
    db_session.add_all([user, org])
    db_session.commit()
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member")
    )
    db_session.commit()
    return user, org


def _entries(db, scope: str, user: User, org: Organization) -> list[tuple[str, str]]:
    query = db.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.subject_type == scope)
    if scope == "organization":
        query = query.filter(ErasureLedgerEntry.subject_organization_id == org.id)
    else:
        query = query.filter(ErasureLedgerEntry.subject_user_id == user.id)
    return [(e.subject_type, e.status) for e in query.all()]


def _erase(db, scope: str, user: User, org: Organization) -> dict[str, Any]:
    from app.services import gdpr_erasure_service as gdpr

    if scope == "user":
        return gdpr.erase_user(db, int(user.id))
    if scope == "org_member":
        return gdpr.erase_org_member_data(db, int(user.id), int(org.id))
    return gdpr.erase_organization(db, int(org.id))


@pytest.mark.parametrize("scope", ["organization", "org_member", "user"])
def test_the_ledger_entry_is_pending_when_destruction_begins(db_session, subject, scope):
    user, org = subject
    seen_at_first_destroy: list[list[tuple[str, str]]] = []

    def _probe(db, files, summary) -> None:
        seen_at_first_destroy.append(_entries(db, scope, user, org))

    with (
        patch("app.services.gdpr_erasure_service._purge_files", _probe),
        patch("app.services.gdpr_erasure_service._erase_speaker_voiceprints", return_value=0),
        patch("app.services.opensearch_service.remove_profile_embedding", return_value=None),
        patch("app.services.gdpr_erasure_service.audit_logger", MagicMock()),
    ):
        summary = _erase(db_session, scope, user, org)

    assert seen_at_first_destroy == [[(scope, "pending")]]
    assert summary["ledger_uuid"] is not None
