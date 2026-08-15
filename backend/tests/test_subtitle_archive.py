"""Unit tests for SubtitleService.build_subtitle_archive.

Regression coverage for the async bulk-export path: the worker builds the ZIP in
memory via this helper, so it must produce a valid archive, count exported/skipped
correctly, and never abort the batch when one file has no transcript.
"""

import io
import uuid
import zipfile

import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.services.subtitle_service import SubtitleService


def _make_file(db_session, sample_user, filename: str) -> MediaFile:
    file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename=filename,
        storage_path=f"media/test/{filename}",
        content_type="video/mp4",
        file_size=1024,
        user_id=sample_user.id,
        status="completed",
        is_public=False,
    )
    db_session.add(file)
    db_session.commit()
    db_session.refresh(file)
    return file


def _add_segments(db_session, sample_user, file: MediaFile, n: int = 2) -> None:
    speaker = Speaker(
        uuid=str(uuid.uuid4()),
        user_id=sample_user.id,
        media_file_id=file.id,
        name="SPEAKER_01",
        display_name="Alice",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    for i in range(n):
        db_session.add(
            TranscriptSegment(
                uuid=str(uuid.uuid4()),
                media_file_id=file.id,
                speaker_id=speaker.id,
                start_time=float(i),
                end_time=float(i) + 0.9,
                text=f"Line number {i} of the transcript.",
            )
        )
    db_session.commit()


@pytest.fixture
def file_with_transcript(db_session, sample_user):
    file = _make_file(db_session, sample_user, "with_transcript.mp4")
    _add_segments(db_session, sample_user, file, n=3)
    return file


@pytest.fixture
def file_without_transcript(db_session, sample_user):
    # Completed but no segments -> generators raise -> counts as skipped.
    return _make_file(db_session, sample_user, "no_transcript.mp4")


class TestBuildSubtitleArchive:
    @pytest.mark.parametrize(
        "fmt,expected_ext",
        [("srt", "srt"), ("webvtt", "vtt"), ("txt", "txt")],
    )
    def test_produces_valid_zip_per_format(
        self, db_session, file_with_transcript, fmt, expected_ext
    ):
        base = "with_transcript"
        zip_bytes, exported, skipped = SubtitleService.build_subtitle_archive(
            db_session,
            [(int(file_with_transcript.id), base)],
            fmt,
            include_speakers=True,
        )
        assert exported == 1
        assert skipped == 0
        assert zipfile.is_zipfile(io.BytesIO(zip_bytes))
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert names == [f"{base}.{expected_ext}"]
            assert zf.read(names[0]).decode("utf-8").strip()

    def test_skips_files_without_transcript(
        self, db_session, file_with_transcript, file_without_transcript
    ):
        zip_bytes, exported, skipped = SubtitleService.build_subtitle_archive(
            db_session,
            [
                (int(file_with_transcript.id), "with_transcript"),
                (int(file_without_transcript.id), "no_transcript"),
            ],
            "srt",
            include_speakers=True,
        )
        assert exported == 1
        assert skipped == 1
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.namelist() == ["with_transcript.srt"]

    def test_all_skipped_yields_empty_archive(self, db_session, file_without_transcript):
        zip_bytes, exported, skipped = SubtitleService.build_subtitle_archive(
            db_session,
            [(int(file_without_transcript.id), "no_transcript")],
            "srt",
            include_speakers=True,
        )
        assert exported == 0
        assert skipped == 1
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.namelist() == []

    def test_speaker_labels_present_when_requested(self, db_session, file_with_transcript):
        zip_bytes, exported, _ = SubtitleService.build_subtitle_archive(
            db_session,
            [(int(file_with_transcript.id), "with_transcript")],
            "txt",
            include_speakers=True,
        )
        assert exported == 1
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            content = zf.read("with_transcript.txt").decode("utf-8")
        assert "Alice" in content
