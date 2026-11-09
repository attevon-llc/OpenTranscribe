"""Account lookups and listings in ``services.user_lookup`` against a real database."""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy import false

from app.services import user_lookup


def found(user):
    assert user is not None
    return user


def test_lookups_resolve_the_same_account_by_each_key(normal_user, db_session):
    assert found(user_lookup.get_user_by_uuid(db_session, normal_user.uuid)).id == normal_user.id
    assert found(user_lookup.get_user_by_id(db_session, normal_user.id)).id == normal_user.id
    assert found(user_lookup.get_user_by_email(db_session, normal_user.email)).id == normal_user.id
    lowered = str(normal_user.email).lower()
    assert found(user_lookup.get_user_by_lower_email(db_session, lowered)).id == normal_user.id
    assert found(user_lookup.get_user_by_email_or_ldap_uid(db_session, normal_user.email)).id == (
        normal_user.id
    )


def test_a_miss_is_none_not_an_error(db_session):
    assert user_lookup.get_user_by_email(db_session, "nobody-here@example.invalid") is None
    assert user_lookup.get_user_by_id(db_session, -1) is None
    assert user_lookup.get_email_by_user_id(db_session, [-1]) == {}


def test_email_match_is_exact_but_lower_email_is_case_insensitive(normal_user, db_session):
    shouted = str(normal_user.email).upper()
    assert user_lookup.get_user_by_email(db_session, shouted) is None
    assert user_lookup.get_users_by_lower_emails(db_session, [shouted.lower()])[0].id == (
        normal_user.id
    )


def test_batch_lookups_return_only_the_requested_accounts(normal_user, admin_user, db_session):
    assert user_lookup.get_email_by_user_id(db_session, [normal_user.id, admin_user.id]) == {
        normal_user.id: normal_user.email,
        admin_user.id: admin_user.email,
    }
    assert [u.id for u in user_lookup.get_users_by_uuids(db_session, [str(admin_user.uuid)])] == [
        admin_user.id
    ]


def test_first_super_admin_is_the_lowest_id_super_admin(super_admin_user, db_session):
    assert found(user_lookup.get_first_super_admin(db_session)).role == "super_admin"


def test_conflict_checks_exclude_the_account_being_edited(normal_user, admin_user, db_session):
    assert (
        user_lookup.find_other_user_by_column(
            db_session, "email", normal_user.email, normal_user.id
        )
        is None
    )
    assert (
        found(
            user_lookup.find_other_user_by_column(
                db_session, "email", normal_user.email, admin_user.id
            )
        ).id
        == normal_user.id
    )
    assert (
        found(
            user_lookup.find_other_user_by_email_ci(
                db_session, str(normal_user.email).upper(), admin_user.id
            )
        ).id
        == normal_user.id
    )
    assert (
        user_lookup.find_other_user_by_email_ci(
            db_session, str(normal_user.email).upper(), normal_user.id
        )
        is None
    )


def test_paged_listings_honour_offset_and_limit(normal_user, admin_user, db_session):
    first = user_lookup.list_users_by_id(db_session, 0, 1)
    second = user_lookup.list_users_by_id(db_session, 1, 1)
    assert len(first) == 1 and len(second) == 1
    assert first[0].id < second[0].id
    assert len(user_lookup.list_users_by_name(db_session, 0, 1)) == 1


def test_approval_queue_lists_only_the_requested_status(normal_user, db_session):
    normal_user.approval_status = "pending"
    db_session.commit()
    pending = user_lookup.list_users_by_approval_status(db_session, "pending", 0, 500)
    assert normal_user.id in {u.id for u in pending}
    assert all(u.approval_status == "pending" for u in pending)


def test_admin_search_applies_every_filter(normal_user, admin_user, db_session):
    rows, total = user_lookup.search_users_paginated(
        db_session,
        text_query=str(admin_user.email),
        role="admin",
        auth_type=None,
        is_active=True,
        offset=0,
        limit=10,
    )
    assert [u.id for u in rows] == [admin_user.id]
    assert total == 1
    rows, total = user_lookup.search_users_paginated(
        db_session,
        text_query=str(admin_user.email),
        role="user",
        auth_type=None,
        is_active=None,
        offset=0,
        limit=10,
    )
    assert (rows, total) == ([], 0)


def test_directory_search_excludes_the_caller_and_matches_personal_scope(
    normal_user, admin_user, db_session
):
    found = user_lookup.search_directory(
        db_session,
        text_query="testadmin_",
        caller_user_id=normal_user.id,
        org_id=None,
        is_org_context=False,
    )
    assert admin_user.id in {u.id for u in found}
    own = user_lookup.search_directory(
        db_session,
        text_query=str(admin_user.email),
        caller_user_id=admin_user.id,
        org_id=None,
        is_org_context=False,
    )
    assert admin_user.id not in {u.id for u in own}


def test_account_counts_report_total_and_active(normal_user, db_session):
    row = user_lookup.count_user_accounts(db_session, false())
    assert row.total >= 1
    assert row.active <= row.total
    assert row.pwd_expired == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda db, u: user_lookup.get_user_by_uuid(db, u.uuid),
        lambda db, u: user_lookup.list_users_by_id(db, 0, 5),
        lambda db, u: user_lookup.get_first_super_admin(db),
        lambda db, u: user_lookup.get_email_by_user_id(db, [u.id]),
    ],
)
def test_lookups_never_commit(call, normal_user, db_session):
    commits: list[int] = []

    def count(_session):
        commits.append(1)

    event.listen(db_session, "after_commit", count)
    try:
        call(db_session, normal_user)
    finally:
        event.remove(db_session, "after_commit", count)
    assert commits == []
