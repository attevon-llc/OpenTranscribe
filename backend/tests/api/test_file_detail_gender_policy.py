"""Inferred gender in the transcript responses that embed a speaker (#1280, plan D5).

A transcript response carries the file's speakers twice over: the ``speakers`` list and the
full speaker object inside every segment. The speaker, profile and cluster endpoints withhold
a gender the model inferred while the owner's scope has inference off; these tests hold the
file detail, the paginated segments, and the two segment-write responses to the same rule.
Each case has an "on" control, a person-confirmed gender stays visible, and the stored value
is re-read to show it was hidden rather than deleted.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models.media import Speaker
from app.models.media import TranscriptSegment
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

STORED = {"gender": 0.93, "age_range": 0.6}


@pytest.fixture
def world(db_session, normal_user):
    """One file: an inferred-male speaker and a person-confirmed-female one, a segment each."""
    file = make_file(db_session, normal_user)
    inferred = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_00",
        predicted_gender="male",
        gender_confirmed_by_user=False,
        attribute_confidence=dict(STORED),
    )
    confirmed = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_01",
        predicted_gender="female",
        gender_confirmed_by_user=True,
    )
    segments = []
    for i, speaker in enumerate((inferred, confirmed)):
        seg = TranscriptSegment(
            media_file_id=file.id,
            speaker_id=speaker.id,
            start_time=float(i),
            end_time=float(i) + 0.9,
            text=f"segment {i}",
        )
        db_session.add(seg)
        segments.append(seg)
    db_session.commit()
    return {"file": file, "inferred": inferred, "confirmed": confirmed, "segments": segments}


def _speakers_by_name(body: dict) -> dict[str, dict]:
    return {s["name"]: s for s in body["speakers"]}


def _segment_speakers_by_name(body: dict) -> dict[str, dict]:
    return {s["speaker"]["name"]: s["speaker"] for s in body["transcript_segments"]}


def _assert_shown(speaker: dict) -> None:
    assert speaker["predicted_gender"] == "male"
    assert speaker["attribute_confidence"] == STORED


def _assert_withheld(speaker: dict) -> None:
    assert speaker["predicted_gender"] is None
    assert speaker["attribute_confidence"] == {"age_range": 0.6}


def test_file_detail_shows_inferred_gender_while_the_switch_is_on(
    client, user_token_headers, world
):
    resp = client.get(f"/api/files/{world['file'].uuid}", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    _assert_shown(_speakers_by_name(body)["SPEAKER_00"])
    _assert_shown(_segment_speakers_by_name(body)["SPEAKER_00"])


def test_file_detail_withholds_inferred_gender_while_off(
    client, user_token_headers, world, db_session
):
    set_instance_switch(db_session, "gender", False)

    resp = client.get(f"/api/files/{world['file'].uuid}", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    _assert_withheld(_speakers_by_name(body)["SPEAKER_00"])
    _assert_withheld(_segment_speakers_by_name(body)["SPEAKER_00"])
    assert _speakers_by_name(body)["SPEAKER_01"]["predicted_gender"] == "female"
    assert _segment_speakers_by_name(body)["SPEAKER_01"]["predicted_gender"] == "female"
    db_session.expire_all()
    assert db_session.get(Speaker, world["inferred"].id).predicted_gender == "male"


def test_segments_page_shows_inferred_gender_while_on_and_withholds_it_while_off(
    client, user_token_headers, world, db_session
):
    url = f"/api/files/{world['file'].uuid}/segments"

    _assert_shown(
        _segment_speakers_by_name(client.get(url, headers=user_token_headers).json())["SPEAKER_00"]
    )

    set_instance_switch(db_session, "gender", False)
    body = client.get(url, headers=user_token_headers).json()

    _assert_withheld(_segment_speakers_by_name(body)["SPEAKER_00"])
    assert _segment_speakers_by_name(body)["SPEAKER_01"]["predicted_gender"] == "female"


def test_segment_text_edit_response_withholds_inferred_gender_while_off(
    client, user_token_headers, world, db_session
):
    seg = world["segments"][0]
    url = f"/api/files/{world['file'].uuid}/transcript/segments/{seg.uuid}"

    on = client.put(url, headers=user_token_headers, json={"text": "edited once"})
    assert on.status_code == status.HTTP_200_OK, on.text
    _assert_shown(on.json()["speaker"])

    set_instance_switch(db_session, "gender", False)
    off = client.put(url, headers=user_token_headers, json={"text": "edited twice"})

    assert off.status_code == status.HTTP_200_OK, off.text
    _assert_withheld(off.json()["speaker"])


def test_segment_speaker_reassignment_response_withholds_inferred_gender_while_off(
    client, user_token_headers, world, db_session
):
    seg = world["segments"][1]
    url = f"/api/transcripts/segments/{seg.uuid}/speaker"
    body = {"speaker_uuid": str(world["inferred"].uuid)}

    on = client.put(url, headers=user_token_headers, json=body)
    assert on.status_code == status.HTTP_200_OK, on.text
    _assert_shown(on.json()["speaker"])

    set_instance_switch(db_session, "gender", False)
    off = client.put(url, headers=user_token_headers, json=body)

    assert off.status_code == status.HTTP_200_OK, off.text
    _assert_withheld(off.json()["speaker"])


def test_file_detail_withholds_the_voice_versus_name_alignment_while_off(
    client, user_token_headers, world, db_session
):
    """The alignment compares the inferred gender with a name's: it is inferred gender too."""
    world["inferred"].attribute_confidence = {
        **STORED,
        "alignment": "mismatch",
        "alignment_hint": "Zylofenix Quorra",
    }
    db_session.commit()
    url = f"/api/files/{world['file'].uuid}"

    on = client.get(url, headers=user_token_headers).json()
    assert _speakers_by_name(on)["SPEAKER_00"]["gender_alignment"] == "mismatch"

    set_instance_switch(db_session, "gender", False)
    off = client.get(url, headers=user_token_headers).json()

    for speaker in (
        _speakers_by_name(off)["SPEAKER_00"],
        _segment_speakers_by_name(off)["SPEAKER_00"],
    ):
        assert speaker["gender_alignment"] is None
        assert speaker["gender_alignment_hint"] is None
