"""A profile rename reaches only the speakers whose link to the profile is visible.

A link a voiceprint made is hidden while its file's voiceprints are off. Writing the profile's
new name into such a speaker would leave a name that only the hidden link explains, and a purge
keeps a name whose provenance is not the voiceprint's. Each case has an "on" control.
"""

from __future__ import annotations

import pytest

from app.core import constants
from app.models.prompt import UserSetting
from app.services.speaker_profile_rename import apply_profile_name_to_speakers
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _rename(db, user, source, *, off):
    profile = make_profile(db, user, "Alice")
    speaker = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="alice",
        profile_id=profile.id,
        verified=True,
        identity_source=source,
    )
    db.commit()
    if off:
        db.add(
            UserSetting(
                user_id=user.id,
                setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
                setting_value="false",
            )
        )
        db.commit()
    renames = apply_profile_name_to_speakers(db, profile.id, "Alice Smith")
    return speaker, renames


@pytest.mark.parametrize(
    ("source", "off", "reached"),
    [
        ("voiceprint_link", False, True),
        ("voiceprint_link", True, False),
        ("voiceprint", False, True),
        ("voiceprint", True, False),
        ("human", True, True),
        (None, True, True),
    ],
)
def test_a_rename_reaches_a_member_only_when_its_link_is_visible(
    db_session, normal_user, source, off, reached
):
    speaker, renames = _rename(db_session, normal_user, source, off=off)

    assert speaker.display_name == ("Alice Smith" if reached else "alice")
    assert len(renames) == (1 if reached else 0), "no index change is announced for a skipped one"
