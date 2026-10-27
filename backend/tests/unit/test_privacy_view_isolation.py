"""``PrivacyView`` reads and per-file consent behave under failure and hooks (#1280).

* A failed scope read must not poison the caller's transaction: PostgreSQL aborts the whole
  transaction on a failed statement, and the view runs inside a request or task step.
* A consent hook that refuses ONE file of a scope must turn that file off and no other file
  of the same (owner, organization) scope. The memo used to be keyed by scope alone, so the
  first file asked decided for every later one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.media import MediaFile
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from app.services.privacy.consent import register_voiceprint_consent_hook
from app.services.privacy.policy import reset_privacy_ceiling_resolver
from app.services.privacy.redact_view import PrivacyView
from tests.fixtures.privacy_switches import file_in


@pytest.fixture(autouse=True)
def _clean_policy_state():
    clear_voiceprint_consent_hooks()
    reset_privacy_ceiling_resolver()
    yield
    clear_voiceprint_consent_hooks()
    reset_privacy_ceiling_resolver()


def test_a_failed_scope_read_leaves_the_callers_transaction_usable(
    db_session, normal_user, monkeypatch
):
    media_file = file_in(db_session, normal_user, None)
    real_query = db_session.query

    def failing_query(*entities, **kwargs):
        if entities and entities[0] is MediaFile.id:
            db_session.execute(text("SELECT 1/0"))  # a real statement error: aborts the txn
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(db_session, "query", failing_query)
    view = PrivacyView(db_session)

    assert view.voiceprints(media_file.id).enabled is False  # fail closed
    monkeypatch.setattr(db_session, "query", real_query)

    assert db_session.execute(text("SELECT 1")).scalar() == 1  # the transaction still works
    assert PrivacyView(db_session).voiceprints(media_file.id).enabled is True  # control


def test_a_consent_hook_that_refuses_one_file_turns_off_that_file_only(db_session, normal_user):
    refused = file_in(db_session, normal_user, None)
    allowed = file_in(db_session, normal_user, None)
    register_voiceprint_consent_hook(lambda db, request: request.media_file_id != refused.id)

    refused_first = PrivacyView(db_session)
    assert refused_first.voiceprints(refused.id).enabled is False
    assert refused_first.voiceprints(allowed.id).enabled is True

    allowed_first = PrivacyView(db_session)
    assert allowed_first.voiceprints(allowed.id).enabled is True
    assert allowed_first.voiceprints(refused.id).enabled is False


def test_without_hooks_one_policy_resolution_serves_every_file_of_a_scope(
    db_session, normal_user, monkeypatch
):
    from app.services.privacy import policy

    files = [file_in(db_session, normal_user, None) for _ in range(5)]
    asked: list[int | None] = []
    real = policy.voiceprint_decision

    def counting(db, **kwargs):
        asked.append(kwargs.get("media_file_id"))
        return real(db, **kwargs)

    monkeypatch.setattr(policy, "voiceprint_decision", counting)
    view = PrivacyView(db_session)

    assert all(view.voiceprints(f.id).enabled for f in files)
    assert len(asked) == 1  # the memo still holds when no hook can tell files apart
