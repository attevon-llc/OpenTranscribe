"""The topic-extraction transcript names a speaker the way every label printer does.

The text is sent to the owner's LLM provider, which can be a remote service. It used to print the
raw ``display_name`` column, so a name a voiceprint made reached the prompt while the file's
voiceprints were off. A person's own name, and the diarizer label of an unnamed speaker, are
unaffected.
"""

from __future__ import annotations

import pytest

from app.core import constants
from app.models.media import TranscriptSegment
from app.models.prompt import UserSetting
from app.services.topic_extraction_service import TopicExtractionService
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _transcript(db, user, *, identity_source, off) -> str:
    media_file = make_file(db, user)
    speaker = make_speaker(
        db,
        user,
        media_file,
        display_name="Alice",
        verified=True,
        identity_source=identity_source,
    )
    db.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.0,
            text="hello there",
        )
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
    text = TopicExtractionService(db)._get_transcript_text(media_file, db=db)
    assert text is not None
    return text


@pytest.mark.parametrize(
    ("identity_source", "off", "expected"),
    [
        ("voiceprint", False, "Alice: hello there"),
        ("voiceprint", True, "SPEAKER_00: hello there"),
        ("human", True, "Alice: hello there"),
        ("voiceprint_link", True, "Alice: hello there"),
        (None, True, "Alice: hello there"),
    ],
)
def test_the_prompt_transcript_names_a_speaker_by_the_files_voiceprint_decision(
    db_session, normal_user, identity_source, off, expected
):
    text = _transcript(db_session, normal_user, identity_source=identity_source, off=off)

    assert text == expected
