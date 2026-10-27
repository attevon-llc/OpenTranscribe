"""The speaker-attribute settings form reports what will actually happen (#1280).

``GET /user-settings/speaker-attributes`` is documented as showing the effective choice, the
same resolver the pipeline uses. The resolver ANDs in the organization's gender ceiling, but
the endpoint resolved for personal scope, so a user whose organization forbids gender
inference saw the toggle on while no gender was ever inferred. The endpoint now resolves for
the caller's active organization and names the layer that locks the choice.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import status

from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.privacy_switches import set_user_switch

URL = "/api/user-settings/speaker-attributes"


def _get(client, headers) -> dict[str, Any]:
    response = client.get(URL, headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
def in_org(org_context, normal_user, db_session):
    """Make the caller act inside an organization with the given gender ceiling."""

    def _enter(*, gender: bool):
        org = make_org(db_session, gender=gender)
        db_session.commit()
        org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
        return org

    return _enter


def test_an_organization_that_forbids_gender_is_reported_as_the_lock(
    client, user_token_headers, in_org
):
    in_org(gender=False)

    body = _get(client, user_token_headers)

    assert body["gender_detection_enabled"] is False
    assert body["gender_locked_by"] == "organization"


def test_an_organization_that_allows_gender_leaves_the_choice_open(
    client, user_token_headers, in_org
):
    in_org(gender=True)

    body = _get(client, user_token_headers)

    assert body["gender_detection_enabled"] is True
    assert body["gender_locked_by"] is None


def test_personal_scope_never_reads_an_organization_policy(client, user_token_headers, db_session):
    """An organization that forbids gender does not bind a user who is not acting in it."""
    make_org(db_session, gender=False)
    db_session.commit()

    body = _get(client, user_token_headers)

    assert body["gender_detection_enabled"] is True
    assert body["gender_locked_by"] is None


def test_the_users_own_switch_is_not_reported_as_a_lock(
    client, user_token_headers, normal_user, db_session
):
    set_user_switch(db_session, normal_user, "gender", False)

    body = _get(client, user_token_headers)

    assert body["gender_detection_enabled"] is False
    assert body["gender_locked_by"] is None


def test_the_instance_switch_is_reported_as_the_lock(client, user_token_headers, db_session):
    set_instance_switch(db_session, "gender", False)

    body = _get(client, user_token_headers)

    assert body["gender_detection_enabled"] is False
    assert body["gender_locked_by"] == "instance"


def test_a_put_response_carries_the_same_lock(client, user_token_headers, in_org):
    in_org(gender=False)

    response = client.put(URL, headers=user_token_headers, json={"gender_detection_enabled": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["gender_detection_enabled"] is False
    assert response.json()["gender_locked_by"] == "organization"
