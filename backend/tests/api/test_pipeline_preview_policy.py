"""A pipeline preview is judged in the organization the request acts in (#1280).

``GET /user-settings/pipeline`` and the ASR-side switches preview the plan the caller's next
file would get. The privacy policy is scoped by (owner, organization), but the preview
loaded its inputs in personal scope, which never reads an organization policy: a user acting
in an organization that forbids voiceprints saw the speaker-recognition step enabled while
every file they uploaded would be planned without it. A planned REAL file keeps using its own
row's organization, never the request's.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import status
from starlette.requests import Request

from app.services.pipeline_plan_inputs import load_plan_inputs
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org

URL = "/api/user-settings/pipeline"


def _speaker_id(body: dict) -> dict:
    return next(step for step in body["steps"] if step["step"] == "speaker_id")


def _get(client, headers) -> dict[str, Any]:
    response = client.get(URL, headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict[str, Any] = response.json()
    return body


def _request() -> Request:
    """A request with no identity of its own; the fixture decides which organization it acts in."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.fixture
def acting_in(org_context, normal_user, db_session):
    """Make the caller act inside a new organization with the given voiceprint switch."""

    def _enter(*, voiceprints: bool):
        org = make_org(db_session, voiceprints=voiceprints)
        db_session.commit()
        org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
        return org

    return _enter


def test_the_preview_shows_speaker_recognition_off_when_the_organization_forbids_it(
    client, user_token_headers, acting_in
):
    acting_in(voiceprints=False)

    body = _get(client, user_token_headers)

    assert _speaker_id(body)["enabled"] is False
    assert "voiceprints_disabled" in [issue["code"] for issue in body["issues"]]


def test_the_preview_shows_speaker_recognition_on_when_the_organization_allows_it(
    client, user_token_headers, acting_in
):
    """The control: same request, same user, only the organization's switch differs."""
    acting_in(voiceprints=True)

    body = _get(client, user_token_headers)

    assert _speaker_id(body)["enabled"] is True
    assert "voiceprints_disabled" not in [issue["code"] for issue in body["issues"]]


def test_a_user_outside_any_organization_is_not_bound_by_one(
    client, user_token_headers, db_session
):
    make_org(db_session, voiceprints=False)
    db_session.commit()

    body = _get(client, user_token_headers)

    assert _speaker_id(body)["enabled"] is True


def test_the_asr_switch_previews_use_the_same_scope(acting_in, normal_user, db_session):
    """The ASR switches call ``plan_for_user`` with the request, as the settings route does."""
    from app.services.pipeline_plan_inputs import plan_for_user

    acting_in(voiceprints=False)

    plan = plan_for_user(db_session, normal_user.id, request=_request())

    assert plan.speaker_id.runs is False
    assert "voiceprints_disabled" in [issue.code for issue in plan.issues]


def test_a_planned_file_uses_its_own_organization_not_the_requests(
    org_context, normal_user, db_session
):
    off_org = make_org(db_session, voiceprints=False)
    on_org = make_org(db_session, voiceprints=True)
    db_session.commit()
    personal_file = file_in(db_session, normal_user, None)
    off_file = file_in(db_session, normal_user, off_org)
    db_session.commit()
    org_context(org_id=on_org.id, org_role="org:member", only_for=normal_user.id)
    request = _request()

    def planned(media_file):
        return load_plan_inputs(
            db_session,
            normal_user.id,
            request=request,
            organization_id=media_file.organization_id,
            media_file_id=int(media_file.id),
        ).voiceprints_allowed

    assert planned(personal_file) is True
    assert planned(off_file) is False
