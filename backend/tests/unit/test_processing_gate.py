"""The legal processing gate's decision (``app.legal.processing_gate``), against real rows."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

import pytest

from app.core.constants import LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY
from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.legal import processing_gate
from app.legal import registry
from app.legal.acceptance import record_acceptance
from app.legal.processing_gate import REASON_ACCEPTANCE_REQUIRED
from app.legal.processing_gate import REASON_DOCUMENT_UNAVAILABLE
from app.legal.processing_gate import REASON_GATE_UNAVAILABLE
from app.legal.processing_gate import check_processing_allowed
from app.models.system_settings import SystemSettings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

pytestmark = pytest.mark.xdist_group("legal_processing_gate_settings")

TERMS = "opentranscribe-terms-of-use"
PRIVACY = "opentranscribe-privacy-notice"


def _configure(db, keys: str, scope: str | None = None) -> None:
    db.add(SystemSettings(key=LEGAL_PROCESSING_REQUIRED_KEYS_KEY, value=keys))
    if scope is not None:
        db.add(SystemSettings(key=LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY, value=scope))
    db.commit()


def _accept(db, user, key: str) -> None:
    doc = registry.get_document(key)
    assert doc is not None, f"{key} is not a vendored document"
    record_acceptance(db, user.id, doc, None)


def _check(db, user, organization_id=None):
    return check_processing_allowed(db, owner_user_id=user.id, organization_id=organization_id)


def test_off_by_default(db_session, normal_user):
    assert _check(db_session, normal_user) is None


def test_an_empty_list_is_off(db_session, normal_user):
    _configure(db_session, " , ")
    assert _check(db_session, normal_user) is None


def test_on_without_acceptance_refuses_with_the_missing_documents(db_session, normal_user):
    _configure(db_session, f"{TERMS},{PRIVACY}")
    _accept(db_session, normal_user, PRIVACY)
    refusal = _check(db_session, normal_user)
    assert refusal is not None
    assert refusal.code == REASON_ACCEPTANCE_REQUIRED
    assert refusal.document_keys == (TERMS,)


def test_the_owners_acceptance_passes(db_session, normal_user):
    _configure(db_session, f"{TERMS},{PRIVACY}")
    _accept(db_session, normal_user, TERMS)
    _accept(db_session, normal_user, PRIVACY)
    assert _check(db_session, normal_user) is None


def test_the_subject_is_the_owner_never_the_actor(db_session, normal_user, admin_user):
    """An admin's own acceptance does not cover a member's file (the owner is the subject)."""
    _configure(db_session, TERMS)
    _accept(db_session, admin_user, TERMS)
    refusal = _check(db_session, normal_user)
    assert refusal is not None and refusal.code == REASON_ACCEPTANCE_REQUIRED
    assert _check(db_session, admin_user) is None


def test_a_changed_document_text_needs_a_fresh_acceptance(db_session, normal_user, monkeypatch):
    _configure(db_session, TERMS)
    _accept(db_session, normal_user, TERMS)
    assert _check(db_session, normal_user) is None
    current = registry.get_document(TERMS)
    assert current is not None
    changed = registry.LegalDocument(
        key=current.key,
        title=current.title,
        version=current.version,
        status=current.status,
        audience=current.audience,
        content=current.content + b"\nAmended.\n",
    )
    monkeypatch.setattr(registry, "get_document", lambda key: changed if key == TERMS else None)
    refusal = _check(db_session, normal_user)
    assert refusal is not None and refusal.code == REASON_ACCEPTANCE_REQUIRED


def test_an_unserved_key_fails_closed(db_session, normal_user):
    _configure(db_session, f"{TERMS},no-such-document")
    _accept(db_session, normal_user, TERMS)
    refusal = _check(db_session, normal_user)
    assert refusal is not None
    assert refusal.code == REASON_DOCUMENT_UNAVAILABLE
    assert refusal.document_keys == ("no-such-document",)


class _BrokenSession:
    def query(self, *a, **kw):
        raise RuntimeError("database unavailable")

    def execute(self, *a, **kw):
        raise RuntimeError("database unavailable")


def test_a_settings_read_that_raises_refuses(normal_user):
    refusal = check_processing_allowed(
        cast("Session", _BrokenSession()), owner_user_id=1, organization_id=None
    )
    assert refusal is not None and refusal.code == REASON_GATE_UNAVAILABLE


def test_an_acceptance_read_that_raises_refuses(db_session, normal_user, monkeypatch):
    _configure(db_session, TERMS)

    def boom(*a, **kw):
        raise RuntimeError("acceptance query failed")

    monkeypatch.setattr(processing_gate, "_user_accepted", boom)
    refusal = _check(db_session, normal_user)
    assert refusal is not None
    assert refusal.code == REASON_GATE_UNAVAILABLE
    assert refusal.document_keys == (TERMS,)


def test_organization_scope_ignores_a_personal_acceptance(db_session, normal_user):
    _configure(db_session, TERMS, scope="organization")
    _accept(db_session, normal_user, TERMS)
    refusal = _check(db_session, normal_user, organization_id=None)
    assert refusal is not None and refusal.code == REASON_ACCEPTANCE_REQUIRED


def test_an_unknown_stored_scope_reads_as_the_narrowest(db_session, normal_user):
    _configure(db_session, TERMS, scope="anyone")
    config = processing_gate.read_config(db_session)
    assert config.scope == "user"
    _accept(db_session, normal_user, TERMS)
    assert _check(db_session, normal_user) is None


def test_refusal_message_names_code_and_documents():
    refusal = processing_gate.ProcessingRefusal(REASON_ACCEPTANCE_REQUIRED, (TERMS,))
    assert refusal.message == f"{REASON_ACCEPTANCE_REQUIRED}: {TERMS}"


def test_the_refusal_error_carries_the_stable_detail():
    err = processing_gate.refusal_error(
        processing_gate.ProcessingRefusal(REASON_ACCEPTANCE_REQUIRED, (TERMS,))
    )
    assert err.status_code == 403
    assert err.detail == {"code": REASON_ACCEPTANCE_REQUIRED, "documents": [TERMS]}
    unavailable = processing_gate.refusal_error(
        processing_gate.ProcessingRefusal(REASON_GATE_UNAVAILABLE, ())
    )
    assert unavailable.status_code == 503
