"""Speaker ``attribute_confidence`` carries non-float keys; every read surface must serve it (#1026).

``speaker.attribute_confidence`` is a JSONB bag with several writers:

* voice-attribute prediction stores ``{"gender": 0.91}``;
* ``_store_metadata_hints_as_suggestions`` adds ``"metadata_hints"``, a LIST of
  ``{name, role, confidence, source}`` dicts;
* ``_store_alignment_results`` adds ``"alignment"`` and ``"alignment_hint"``, both strings.

The ``Speaker`` response schema declared the whole bag ``dict[str, float]``, so any
speaker carrying one of the non-float keys failed validation and the file detail,
segments and speaker-detail endpoints answered 500 for a file that had processed
successfully. The speaker LIST endpoint never failed: it hand-built a dict that split
the bag. These tests pin the contract every surface now shares: ``attribute_confidence``
is the numeric map only, and the other keys are exposed as their own typed fields.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment

HINTS = [
    {"name": "Zylofenix Quorra", "role": "host", "confidence": 0.8, "source": "title"},
    {"name": "Brannoch Vell", "role": "guest", "confidence": 0.7, "source": "description"},
]

# Each stored shape a real writer produces, alone and combined.
STORED_SHAPES = {
    "metadata_hints": {"gender": 0.91, "metadata_hints": HINTS},
    "alignment": {"gender": 0.91, "alignment": "match", "alignment_hint": "Zylofenix Quorra"},
    "all_writers": {
        "gender": 0.91,
        "metadata_hints": HINTS,
        "alignment": "mismatch",
        "alignment_hint": "Brannoch Vell",
    },
}


def _make_file_with_speaker(db_session, owner, attribute_confidence) -> tuple[MediaFile, Speaker]:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="attr_conf_probe.wav",
        title="attr_conf_probe",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    speaker = Speaker(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        predicted_gender="female",
        attribute_confidence=attribute_confidence,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    for i in range(2):
        db_session.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=float(i),
                end_time=float(i) + 0.9,
                text=f"segment {i}",
            )
        )
    db_session.commit()
    return media_file, speaker


def _assert_split_speaker(speaker_json: dict, stored: dict) -> None:
    """The numeric map holds only numbers; every other stored key has its own field."""
    assert speaker_json["attribute_confidence"] == {"gender": 0.91}
    assert speaker_json["metadata_hints"] == stored.get("metadata_hints", [])
    assert speaker_json["gender_alignment"] == stored.get("alignment")
    assert speaker_json["gender_alignment_hint"] == stored.get("alignment_hint")


@pytest.mark.parametrize("shape", sorted(STORED_SHAPES))
def test_file_detail_serves_a_speaker_with_non_float_attribute_keys(
    shape, client, user_token_headers, normal_user, db_session
):
    stored = STORED_SHAPES[shape]
    media_file, _speaker = _make_file_with_speaker(db_session, normal_user, dict(stored))

    response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    segments = response.json()["transcript_segments"]
    assert len(segments) == 2
    for segment in segments:
        _assert_split_speaker(segment["speaker"], stored)


@pytest.mark.parametrize("shape", sorted(STORED_SHAPES))
def test_segments_endpoint_serves_a_speaker_with_non_float_attribute_keys(
    shape, client, user_token_headers, normal_user, db_session
):
    stored = STORED_SHAPES[shape]
    media_file, _speaker = _make_file_with_speaker(db_session, normal_user, dict(stored))

    response = client.get(f"/api/files/{media_file.uuid}/segments", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    segments = response.json()["transcript_segments"]
    assert len(segments) == 2
    for segment in segments:
        _assert_split_speaker(segment["speaker"], stored)


@pytest.mark.parametrize("shape", sorted(STORED_SHAPES))
def test_speaker_detail_serves_non_float_attribute_keys(
    shape, client, user_token_headers, normal_user, db_session
):
    stored = STORED_SHAPES[shape]
    _media_file, speaker = _make_file_with_speaker(db_session, normal_user, dict(stored))

    response = client.get(f"/api/speakers/{speaker.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    _assert_split_speaker(response.json(), stored)


def test_speaker_list_and_detail_agree_on_the_split(
    client, user_token_headers, normal_user, db_session
):
    """The list endpoint hand-builds its dict and the detail endpoint validates the
    schema; both must come from one split, so they cannot drift apart again."""
    stored = STORED_SHAPES["all_writers"]
    media_file, speaker = _make_file_with_speaker(db_session, normal_user, dict(stored))

    detail = client.get(f"/api/speakers/{speaker.uuid}", headers=user_token_headers)
    listing = client.get(
        "/api/speakers", params={"file_uuid": str(media_file.uuid)}, headers=user_token_headers
    )

    assert detail.status_code == status.HTTP_200_OK, detail.text
    assert listing.status_code == status.HTTP_200_OK, listing.text
    listed = [s for s in listing.json() if s["uuid"] == str(speaker.uuid)]
    assert len(listed) == 1
    for key in (
        "attribute_confidence",
        "metadata_hints",
        "gender_alignment",
        "gender_alignment_hint",
    ):
        assert listed[0][key] == detail.json()[key], key


def test_a_speaker_with_no_attribute_data_still_serializes_as_empty(
    client, user_token_headers, normal_user, db_session
):
    """The control: a speaker that never had attributes predicted keeps null/empty fields."""
    _media_file, speaker = _make_file_with_speaker(db_session, normal_user, None)

    response = client.get(f"/api/speakers/{speaker.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["attribute_confidence"] is None
    assert body["metadata_hints"] == []
    assert body["gender_alignment"] is None
    assert body["gender_alignment_hint"] is None
