"""Issue #284 A2.7 — `format_media_file` validates once, with identical output.

`FormattingService.format_media_file` used to run two full Pydantic passes per row:
``model_validate(orm_row).model_dump()``, mutate the dict, then
``model_validate(dict)``. On a 100-item gallery page that is ~200 validations plus 100
dumps for the sake of six pre-formatted display strings.

It now validates once and applies the display fields with ``model_copy(update=...)``.
These tests pin that the visible result is unchanged, by re-implementing the old
two-pass shape as ``_reference_two_pass`` and asserting the two agree field-for-field
across the interesting states (completed, errored, with and without speakers).
"""

import datetime
import uuid as uuid_mod

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.formatting_service import FormattingService


def _reference_two_pass(media_file, speakers=None) -> MediaFileSchema:
    """The pre-#284 implementation, kept here purely as the equivalence oracle."""
    file_dict = MediaFileSchema.model_validate(media_file).model_dump()

    file_dict["formatted_duration"] = FormattingService.format_duration(
        float(media_file.duration) if media_file.duration is not None else None
    )
    file_dict["formatted_upload_date"] = FormattingService.format_upload_date(
        media_file.upload_time
    )
    file_dict["formatted_file_age"] = FormattingService.format_file_age(media_file.upload_time)
    file_dict["formatted_file_size"] = FormattingService.format_bytes_detailed(
        int(media_file.file_size) if media_file.file_size is not None else None
    )
    file_dict["display_status"] = FormattingService.format_status(media_file.status)
    file_dict["status_badge_class"] = FormattingService.get_status_badge_class(
        media_file.status.value
    )

    if media_file.status == FileStatus.ERROR and hasattr(media_file, "last_error_message"):
        error_info = ErrorCategorizationService.get_error_info(
            str(media_file.last_error_message)
            if media_file.last_error_message is not None
            else None
        )
        file_dict["error_category"] = error_info["category"]
        file_dict["error_suggestions"] = error_info["suggestions"]
        file_dict["is_retryable"] = error_info["is_retryable"]

    if speakers:
        file_dict["speaker_summary"] = FormattingService.create_speaker_summary(speakers)

    return MediaFileSchema.model_validate(file_dict)  # type: ignore[no-any-return]


def _make_media_file(**overrides) -> MediaFile:
    owner = User()
    owner.uuid = uuid_mod.uuid4()

    media_file = MediaFile()
    media_file.id = 1
    media_file.uuid = uuid_mod.uuid4()
    media_file.user = owner
    media_file.filename = "meeting.mp4"
    media_file.storage_path = "user/1/meeting.mp4"
    media_file.upload_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=3)
    media_file.file_size = 987_654_321
    media_file.content_type = "video/mp4"
    media_file.duration = 3612.5
    media_file.language = "en"
    media_file.status = FileStatus.COMPLETED
    media_file.title = "Quarterly review"
    media_file.author = "Recorder"
    media_file.description = "A meeting recording"
    media_file.media_format = "mp4"
    media_file.codec = "h264"
    media_file.resolution_width = 1920
    media_file.resolution_height = 1080
    media_file.frame_rate = 29.97
    media_file.last_error_message = None
    for key, value in overrides.items():
        setattr(media_file, key, value)
    return media_file


def _make_speakers(count: int) -> list[Speaker]:
    speakers = []
    for index in range(count):
        speaker = Speaker()
        speaker.id = index
        speaker.uuid = uuid_mod.uuid4()
        speaker.name = f"SPEAKER_{index:02d}"
        speaker.display_name = f"Person {index}" if index % 2 == 0 else None
        speakers.append(speaker)
    return speakers


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "speaker_count"),
    [
        ({}, 0),
        ({}, 3),
        ({"duration": None, "file_size": None}, 0),
        ({"status": FileStatus.PROCESSING}, 2),
        ({"status": FileStatus.QUARANTINED}, 0),
        (
            {
                "status": FileStatus.ERROR,
                "last_error_message": "CUDA out of memory while loading the model",
            },
            1,
        ),
        ({"status": FileStatus.ERROR, "last_error_message": None}, 0),
    ],
    ids=[
        "completed-no-speakers",
        "completed-with-speakers",
        "missing-duration-and-size",
        "processing",
        "quarantined",
        "error-with-message",
        "error-without-message",
    ],
)
def test_single_validation_matches_the_old_two_pass_output(overrides, speaker_count):
    media_file = _make_media_file(**overrides)
    speakers = _make_speakers(speaker_count)

    expected = _reference_two_pass(media_file, speakers or None)
    actual = FormattingService.format_media_file(media_file, speakers or None)

    assert actual.model_dump() == expected.model_dump()
    # Also compare the serialised wire form — this is what the SPA actually receives.
    assert actual.model_dump_json() == expected.model_dump_json()


@pytest.mark.unit
def test_display_fields_are_populated():
    """Guard against `model_copy(update=...)` silently dropping the update dict."""
    media_file = _make_media_file()
    result = FormattingService.format_media_file(media_file, _make_speakers(2))

    assert result.formatted_duration
    assert result.formatted_upload_date
    assert result.formatted_file_age
    assert result.formatted_file_size
    assert result.display_status == "Completed"
    assert result.status_badge_class
    assert result.speaker_summary == {
        "count": 2,
        "primary_speakers": ["Person 0", "SPEAKER_01"],
    }
    # Untouched fields survive the copy.
    assert result.filename == "meeting.mp4"
    assert result.title == "Quarterly review"


@pytest.mark.unit
def test_rows_do_not_share_mutable_state():
    """`model_copy` is shallow — make sure two formatted rows are still independent."""
    speakers = _make_speakers(2)
    first = FormattingService.format_media_file(_make_media_file(filename="a.mp4"), speakers)
    second = FormattingService.format_media_file(_make_media_file(filename="b.mp4"), speakers)

    first_summary = first.speaker_summary
    second_summary = second.speaker_summary
    assert first_summary is not None and second_summary is not None
    assert first_summary is not second_summary
    first_summary["count"] = 999
    assert second_summary["count"] == 2
    assert first.filename == "a.mp4"
    assert second.filename == "b.mp4"
