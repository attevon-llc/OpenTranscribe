"""The FILE OWNER's gender opt-out binds a run another person triggers (#1280).

``resolve_speaker_attribute_flags_for_file`` resolves the flags for ``user_id`` (defaulting to
the owner). An admin reprocess or a collaborator passes THEIR id, whose own switches are on.
The only thing that keeps the owner's opt-out in force then is the second, owner-scoped
decision at the end of the function. Mutation testing found that line removable with the
suite green; this file is the test that fails without it.

Each "off" case has an "on" control: an owner who has NOT opted out leaves the triggering
user's choice open, so "always deny" cannot pass.
"""

from __future__ import annotations

import uuid as uuid_mod

from app.core import constants
from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.services.speaker_attribute_settings import resolve_speaker_attribute_flags_for_file


def _file_of(db, owner) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=owner.id,
        filename="owner.mp4",
        storage_path=f"test/{uuid_mod.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    return media_file


def _gender_switch(db, user, value: bool) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_GENDER_DETECTION_KEY,
            setting_value="true" if value else "false",
        )
    )
    db.commit()


def test_an_owner_who_opted_out_stays_opted_out_when_an_admin_triggers_the_run(
    db_session, normal_user, admin_user
):
    media_file = _file_of(db_session, normal_user)
    _gender_switch(db_session, normal_user, False)
    _gender_switch(db_session, admin_user, True)

    flags = resolve_speaker_attribute_flags_for_file(
        db_session, media_file.id, user_id=admin_user.id
    )

    assert flags.gender_detection_enabled is False
    assert flags.gender_prediction_allowed is False


def test_an_owner_who_did_not_opt_out_leaves_the_triggering_users_choice_open(
    db_session, normal_user, admin_user
):
    media_file = _file_of(db_session, normal_user)
    _gender_switch(db_session, normal_user, True)
    _gender_switch(db_session, admin_user, True)

    flags = resolve_speaker_attribute_flags_for_file(
        db_session, media_file.id, user_id=admin_user.id
    )

    assert flags.gender_detection_enabled is True
    assert flags.gender_prediction_allowed is True
