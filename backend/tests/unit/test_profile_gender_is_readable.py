"""A confirmed profile gender must be readable, and must win (#543).

Two sources of truth existed for one field name:

* **Write** — ``POST /speaker-profiles/profiles/{uuid}/confirm-gender`` sets
  ``SpeakerProfile.predicted_gender`` and bulk-updates every linked ``Speaker``.
* **Read** — ``GET /speaker-profiles/profiles`` reported the most common
  ``Speaker.predicted_gender`` among the profile's members, via a ``row_number()``
  window. It never read ``SpeakerProfile.predicted_gender`` at all.

The column had exactly **one writer and zero readers**. Confirming a gender on a
profile WITH members appeared to work only because the same call also bulk-sets the
members, which moves the derived majority; on a **member-less** profile the write was
invisible everywhere and the UI toggle (``ProfilesTab.svelte``, ``class:active={profile.
predicted_gender === …}``) could never light up.

Measured on the dev stack before the fix — the write landed and the read denied it:

    POST …/confirm-gender?gender=female  -> 200 {"predicted_gender": "female"}
    SELECT predicted_gender FROM speaker_profile  -> female
    GET  …/profiles                       -> null

The fix reads ``COALESCE(confirmed column, derived majority)``. It is
behaviour-preserving for every profile that has ever been confirmed, because the
confirm writes both and they therefore already agree — verified on live data before
changing anything. A profile that was never confirmed has NULL in the column and still
reports the derived value, byte-identical to before.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _make_file(db, user, *, title="Recording"):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.flush()
    return media


def _make_profile(db, user, *, predicted_gender=None):
    from app.models.media import SpeakerProfile

    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        name=f"P-{uuid_pkg.uuid4().hex[:6]}",
        predicted_gender=predicted_gender,
    )
    db.add(profile)
    db.flush()
    return profile


def _make_speaker(db, user, media, profile, *, gender, name="SPEAKER_00"):
    from app.models.media import Speaker

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        media_file_id=media.id,
        name=name,
        profile_id=profile.id,
        predicted_gender=gender,
    )
    db.add(speaker)
    db.flush()
    return speaker


def _listed(client, uuid: str) -> dict[str, Any]:
    """The API's view of one profile."""
    response = client.get("/api/speaker-profiles/profiles")
    assert response.status_code == 200, response.text
    payload: list[dict[str, Any]] = response.json()
    match = [p for p in payload if p["uuid"] == uuid]
    assert match, f"profile {uuid} missing from the listing"
    return match[0]


def test_a_confirmed_gender_is_visible_on_a_profile_with_no_members(
    client, db_session, normal_user, user_token_headers
):
    """The case the write was silently lost in.

    A brand-new profile has no speakers, so the derived majority is empty and the
    confirmed value was the ONLY information available — and it was discarded.
    """
    profile = _make_profile(db_session, normal_user, predicted_gender="female")
    db_session.flush()

    client.headers.update(user_token_headers)
    assert _listed(client, str(profile.uuid))["predicted_gender"] == "female"


def test_an_unconfirmed_profile_still_reports_the_derived_majority(
    client, db_session, normal_user, user_token_headers
):
    """The control, and the compatibility guarantee.

    A profile that was never confirmed has NULL in the column, so the answer must be
    exactly what it was before this change: the majority over its members.
    """
    profile = _make_profile(db_session, normal_user, predicted_gender=None)
    media = _make_file(db_session, normal_user)
    _make_speaker(db_session, normal_user, media, profile, gender="male", name="SPEAKER_00")
    _make_speaker(db_session, normal_user, media, profile, gender="male", name="SPEAKER_01")
    _make_speaker(db_session, normal_user, media, profile, gender="female", name="SPEAKER_02")
    db_session.flush()

    client.headers.update(user_token_headers)
    assert _listed(client, str(profile.uuid))["predicted_gender"] == "male"


def test_a_confirmed_gender_overrides_a_disagreeing_member_majority(
    client, db_session, normal_user, user_token_headers
):
    """ "Confirm" has to mean something, or the button is decoration.

    Members can drift after a confirmation — a re-diarization can add speakers whose
    voice-inferred gender disagrees. The value a human asserted wins.
    """
    profile = _make_profile(db_session, normal_user, predicted_gender="female")
    media = _make_file(db_session, normal_user)
    _make_speaker(db_session, normal_user, media, profile, gender="male", name="SPEAKER_00")
    _make_speaker(db_session, normal_user, media, profile, gender="male", name="SPEAKER_01")
    db_session.flush()

    client.headers.update(user_token_headers)
    assert _listed(client, str(profile.uuid))["predicted_gender"] == "female"


def test_a_profile_with_neither_reports_nothing(
    client, db_session, normal_user, user_token_headers
):
    """No confirmation and no members with a gender is genuinely unknown.

    Without this, "always fall back to something" could pass the tests above.
    """
    profile = _make_profile(db_session, normal_user, predicted_gender=None)
    media = _make_file(db_session, normal_user)
    _make_speaker(db_session, normal_user, media, profile, gender=None)
    db_session.flush()

    client.headers.update(user_token_headers)
    assert _listed(client, str(profile.uuid))["predicted_gender"] is None


def test_confirming_through_the_endpoint_is_then_readable(
    client, db_session, normal_user, user_token_headers
):
    """End to end: the write and the read must agree about the same profile.

    This is the round trip that was broken — 200 with a value on the way in, null on
    the way out — and it is the one a user actually performs.
    """
    profile = _make_profile(db_session, normal_user, predicted_gender=None)
    db_session.flush()
    client.headers.update(user_token_headers)

    confirmed = client.post(
        f"/api/speaker-profiles/profiles/{profile.uuid}/confirm-gender",
        params={"gender": "female"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["predicted_gender"] == "female"

    assert _listed(client, str(profile.uuid))["predicted_gender"] == "female", (
        "the endpoint reported a value the listing then denied — the #543 round trip"
    )
