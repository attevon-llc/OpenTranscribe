"""A segment edit response carries no speaker name the policy hides (#1280).

``PUT .../transcript/segments/{uuid}`` (text edit) and ``PUT /transcripts/segments/{uuid}/speaker``
(reassign) return the segment WITH its speaker. The stored status fields of a speaker
(``resolved_display_name``, ``status_text``) were computed without the policy, so a response
that serializes them as stored names a person the file's voiceprints no longer allow showing.
The response must redact first and compute the status second, as the file detail does.
Each case has an "on" control.
"""

from __future__ import annotations

import json

import pytest
from fastapi import status

from app.services.privacy import policy
from app.services.speaker_status_service import SpeakerStatusService
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import build_world

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def world(db_session, normal_user):
    built = build_world(db_session, normal_user)
    for speaker in (built.alice, built.carol, built.bob):
        assert SpeakerStatusService.update_speaker_status_in_db(db_session, speaker.id)
    db_session.expire_all()
    # The stored status is the policy-free description of the data: the premise of the test.
    assert ALICE in built.alice.resolved_display_name
    return built


def _edit_text(client, headers, world) -> dict:
    segment = world.segments[0]  # Alice's first segment
    response = client.put(
        f"/api/files/{world.file.uuid}/transcript/segments/{segment.uuid}",
        json={"text": "an edited remark"},
        headers=headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict = response.json()
    return body


def _reassign(client, headers, world) -> dict:
    segment = world.segments[2]  # Bob's first segment, handed to Alice
    response = client.put(
        f"/api/transcripts/segments/{segment.uuid}/speaker",
        json={"speaker_uuid": str(world.alice.uuid)},
        headers=headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict = response.json()
    return body


@pytest.mark.parametrize("edit", [_edit_text, _reassign])
def test_the_response_names_the_voiceprint_speaker_while_voiceprints_are_on(
    client, user_token_headers, world, edit
):
    body = edit(client, user_token_headers, world)

    assert ALICE in json.dumps(body)
    assert body["speaker"]["resolved_display_name"] == ALICE


@pytest.mark.parametrize("edit", [_edit_text, _reassign])
def test_the_response_hides_the_voiceprint_name_everywhere_while_voiceprints_are_off(
    client, user_token_headers, world, db_session, edit
):
    set_instance_switch(db_session, "voiceprints", False)

    body = edit(client, user_token_headers, world)

    assert ALICE not in json.dumps(body)
    assert body["speaker"]["resolved_display_name"] == "SPEAKER_00"
    assert body["speaker"]["display_name"] is None
    assert body["speaker"]["profile_id"] is None
