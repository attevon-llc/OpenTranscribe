"""The admin embedding migration and the voiceprint switches (#1280, plan D5, D8 and Q3).

A migration writes voiceprints, so a scope whose switch is off is skipped. The admin is told
before starting (``policy_skipped``), a run that could write nothing at all is refused with a
409 instead of an empty success, and a run that can write something goes ahead and says what
it will leave out. Counts are asserted for one owner (``user_id=``), so the dev database's
other recordings cannot move them.
"""

from __future__ import annotations

import pytest
from fastapi import status

from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch

EMBEDDING = "/api/embeddings/migration"


@pytest.fixture
def two_scopes(db_session, normal_user):
    """The user's recordings in two scopes: personal (allowed) and an org that forbids it."""
    off_org = make_org(db_session, voiceprints=False)
    file_in(db_session, normal_user, None)
    file_in(db_session, normal_user, off_org)
    file_in(db_session, normal_user, off_org)
    db_session.commit()
    return normal_user


def test_a_start_that_could_write_nothing_is_refused_with_the_layer(
    client, super_admin_token_headers, two_scopes, db_session
):
    set_instance_switch(db_session, "voiceprints", False)

    response = client.post(
        f"{EMBEDDING}/start",
        params={"force": True, "user_id": two_scopes.id},
        headers=super_admin_token_headers,
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == {"code": "voiceprints_disabled", "locked_by": "instance"}


def test_a_start_with_some_allowed_scopes_goes_ahead_and_names_what_it_skips(
    client, super_admin_token_headers, two_scopes
):
    response = client.post(
        f"{EMBEDDING}/start",
        params={"force": True, "user_id": two_scopes.id},
        headers=super_admin_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["status"] == "started"
    assert body["policy_skipped"] == {
        "files_total": 3,
        "files_skipped": 2,
        "scopes_total": 2,
        "scopes_skipped": 1,
        "locked_by": ["organization"],
    }


def test_a_start_with_nothing_forbidden_carries_no_warning(
    client, super_admin_token_headers, normal_user, db_session
):
    file_in(db_session, normal_user, None)
    db_session.commit()

    response = client.post(
        f"{EMBEDDING}/start",
        params={"force": True, "user_id": normal_user.id},
        headers=super_admin_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["status"] == "started"
    assert "policy_skipped" not in response.json()


def test_the_status_warns_before_a_start_when_a_switch_is_off(
    client, super_admin_token_headers, db_session
):
    set_instance_switch(db_session, "voiceprints", False)

    response = client.get(f"{EMBEDDING}/status", headers=super_admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    skipped = response.json()["policy_skipped"]
    assert skipped["files_skipped"] == skipped["files_total"]
    assert skipped["locked_by"] == ([] if skipped["files_total"] == 0 else ["instance"])
