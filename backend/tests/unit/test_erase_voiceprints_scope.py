"""``_erase_speaker_voiceprints`` scopes: the erasure's own, and the privacy purge's 'personal'.

The function is the one place the speaker indices are enumerated and a ``delete_by_query`` is
built. The privacy purge reuses it with ``personal_only`` (documents carrying no organization),
so these pin that a scope reaches exactly its own documents and that erasure's existing scopes
(a user, an organization, one member of one organization) did not move.
"""

from __future__ import annotations

import pytest

from app.services.gdpr_erasure_service import _erase_speaker_voiceprints
from tests.fixtures.fake_speaker_index import FakeOpenSearch


@pytest.fixture()
def index(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    for doc_id, user, org in (
        ("a-org1", 1, 10),
        ("a-org2", 1, 20),
        ("a-personal", 1, None),
        ("b-org1", 2, 10),
        ("b-personal", 2, None),
    ):
        fake.add(doc_id, user, org)
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    return fake


def test_a_user_erasure_takes_all_of_that_users_documents(index):
    assert _erase_speaker_voiceprints(user_id=1) == 3
    assert index.ids() == {"b-org1", "b-personal"}


def test_an_organization_erasure_takes_every_members_documents_in_it(index):
    assert _erase_speaker_voiceprints(organization_id=10) == 2
    assert index.ids() == {"a-org2", "a-personal", "b-personal"}


def test_one_member_of_one_organization(index):
    assert _erase_speaker_voiceprints(user_id=1, organization_id=10) == 1
    assert "a-org1" not in index.ids() and "b-org1" in index.ids()


def test_personal_only_takes_every_users_personal_documents_and_no_tenant_document(index):
    assert _erase_speaker_voiceprints(personal_only=True) == 2
    assert index.ids() == {"a-org1", "a-org2", "b-org1"}


def test_personal_only_for_one_user(index):
    assert _erase_speaker_voiceprints(user_id=2, personal_only=True) == 1
    assert index.ids() == {"a-org1", "a-org2", "a-personal", "b-org1"}


def test_no_scope_deletes_nothing(index):
    assert _erase_speaker_voiceprints() == 0
    assert len(index.docs) == 5


def test_personal_only_with_an_organization_is_a_contradiction_that_deletes_nothing(index):
    assert _erase_speaker_voiceprints(personal_only=True, organization_id=10) == 0
    assert len(index.docs) == 5
