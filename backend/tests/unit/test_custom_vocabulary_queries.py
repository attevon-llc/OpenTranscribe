"""Tenant-scoped vocabulary queries in ``services.custom_vocabulary_queries``."""

from __future__ import annotations

from app.models.custom_vocabulary import CustomVocabulary
from app.services import custom_vocabulary_queries as q


def _term(db_session, user_id, term, domain="general", *, org=None, active=True):
    row = CustomVocabulary(
        user_id=user_id, organization_id=org, term=term, domain=domain, is_active=active
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_own_listing_is_ordered_and_filters_by_domain_and_activity(normal_user, db_session):
    _term(db_session, normal_user.id, "beta", "legal")
    _term(db_session, normal_user.id, "alpha", "legal")
    _term(db_session, normal_user.id, "gamma", "medical", active=False)
    everything = q.list_own_vocabulary(db_session, normal_user.id, None)
    assert [t.term for t in everything] == ["alpha", "beta", "gamma"]
    assert [
        t.term for t in q.list_own_vocabulary(db_session, normal_user.id, None, domain="legal")
    ] == [
        "alpha",
        "beta",
    ]
    active = q.list_own_vocabulary(db_session, normal_user.id, None, active_only=True)
    assert "gamma" not in [t.term for t in active]


def test_own_terms_are_tenant_scoped(normal_user, db_session):
    _term(db_session, normal_user.id, "personal", org=None)
    assert q.list_own_vocabulary(db_session, normal_user.id, 999) == []
    assert q.own_term_pairs(db_session, normal_user.id, None) == {("personal", "general")}
    assert q.own_term_pairs(db_session, normal_user.id, 999) == set()


def test_shared_terms_are_owner_less_and_never_include_user_terms(normal_user, db_session):
    _term(db_session, normal_user.id, "mine")
    shared = _term(db_session, None, "system-term-zz")
    listed = q.list_shared_vocabulary(db_session, None)
    assert shared.id in [t.id for t in listed]
    assert all(t.user_id is None for t in listed)


def test_duplicate_and_clash_checks_exclude_the_row_itself(normal_user, db_session):
    first = _term(db_session, normal_user.id, "dup")
    second = _term(db_session, normal_user.id, "other")
    found = q.find_own_term(db_session, normal_user.id, None, "dup", "general")
    assert found is not None and found.id == first.id
    assert q.own_term_clashes(db_session, normal_user.id, None, "dup", "general", second.id)
    assert not q.own_term_clashes(db_session, normal_user.id, None, "dup", "general", first.id)


def test_delete_own_can_be_limited_to_one_domain_and_spares_others_terms(
    normal_user, admin_user, db_session
):
    _term(db_session, normal_user.id, "a", "legal")
    _term(db_session, normal_user.id, "b", "medical")
    theirs = _term(db_session, admin_user.id, "a", "legal")
    q.delete_own_vocabulary(db_session, normal_user.id, None, domain="legal")
    db_session.expire_all()
    assert {t.term for t in q.list_own_vocabulary(db_session, normal_user.id, None)} == {"b"}
    assert q.find_vocabulary_term(db_session, theirs.id) is not None
    q.delete_own_vocabulary(db_session, normal_user.id, None)
    db_session.expire_all()
    assert q.list_own_vocabulary(db_session, normal_user.id, None) == []
