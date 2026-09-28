"""The recording's true media duration, end to end (issues #969 and #974).

Two compounding defects made ``MediaFile.duration`` — and therefore the
``CompletionContext.audio_duration_s`` the transcription-complete hook receives —
report the transcript's speech extent instead of the recording's length:

* #974: PyExifTool returns group-prefixed keys (``Composite:Duration`` for a WAV), which
  the exact-key allowlist missed, so an audio upload never got a container duration.
  ExifTool also has no duration for Ogg/Opus and only an *estimate* for an MP3 without a
  Xing header, so a local file is additionally probed with ffprobe.
* #969: completion then overwrote whatever duration there was with the last segment's
  end, discarding any trailing silence.

Every media file here is REAL: derived with ffmpeg from the committed
``fixtures/media`` clips, padded with trailing silence so speech extent and media length
disagree by a known amount. ffmpeg and exiftool are both required — the three production
images install them, and so does the CI job.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid as uuid_module
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.tasks.transcription import metadata_extractor
from app.tasks.transcription.hooks import clear_hooks
from app.tasks.transcription.hooks import register_transcription_complete
from app.tasks.transcription.metadata_extractor import extract_media_metadata
from app.tasks.transcription.metadata_extractor import get_important_metadata
from app.tasks.transcription.postprocess import _fire_completion_metering
from app.tasks.transcription.preprocess import _extract_metadata_best_effort
from app.tasks.transcription.storage import update_media_file_transcription_status

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "media"
SOURCE_WAV = FIXTURES / "sample_short.wav"  # 10.0 s of speech-bearing audio
SOURCE_MP4 = FIXTURES / "sample_short.mp4"  # 5.0 s video
PAD_S = 1.5
PADDED_AUDIO_S = 11.5  # SOURCE_WAV + PAD_S of silence
PADDED_VIDEO_S = 6.5  # SOURCE_MP4 + PAD_S of frozen frame and silence
#: ExifTool's bitrate-based estimate for PADDED mp3 — measured 9.3096 s.
EXIFTOOL_MP3_ESTIMATE_CEILING = 10.0

_PREPROCESS = "app.tasks.transcription.preprocess"


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise RuntimeError(f"{binary} is required for these tests (the production images ship it)")
    return path


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        [_require("ffmpeg"), "-hide_banner", "-v", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def padded(tmp_path_factory) -> dict[str, Path]:
    """Real media with trailing silence, in each container the defects differ on."""
    _require("exiftool")
    out = tmp_path_factory.mktemp("padded_media")
    wav = out / "padded.wav"
    _ffmpeg("-i", str(SOURCE_WAV), "-af", f"apad=pad_dur={PAD_S}", str(wav))
    files = {"wav": wav}
    for ext, codec in (
        ("ogg", ["-c:a", "libopus", "-b:a", "24k"]),
        ("mp3", ["-c:a", "libmp3lame", "-b:a", "32k"]),
        ("flac", ["-c:a", "flac"]),
    ):
        files[ext] = out / f"padded.{ext}"
        _ffmpeg("-i", str(wav), *codec, str(files[ext]))
    files["mp4"] = out / "padded.mp4"
    _ffmpeg(
        "-i",
        str(SOURCE_MP4),
        "-vf",
        f"tpad=stop_mode=clone:stop_duration={PAD_S}",
        "-af",
        f"apad=pad_dur={PAD_S}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-t",
        str(PADDED_VIDEO_S),
        str(files["mp4"]),
    )
    return files


@pytest.fixture
def transcript_segments() -> list[dict[str, Any]]:
    """The committed 2-speaker transcript; its speech ends at 9.96 s."""
    data = json.loads((FIXTURES / "sample_transcript.json").read_text())
    segments: list[dict[str, Any]] = data["segments"]
    return segments


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def media_row(db_session, normal_user):
    """Build a fresh upload row (duration unknown, as upload.py creates it)."""

    def _make(filename: str, content_type: str) -> MediaFile:
        mf = MediaFile(
            uuid=uuid_module.uuid4(),
            user_id=normal_user.id,
            filename=filename,
            storage_path=f"user_{normal_user.id}/{filename}",
            file_size=1,
            content_type=content_type,
            status=FileStatus.PROCESSING,
            duration=None,
        )
        db_session.add(mf)
        db_session.commit()
        db_session.refresh(mf)
        return mf

    return _make


def _run_preprocess_metadata(db_session, mf: MediaFile, path: Path) -> None:
    """Run the real preprocess metadata step against a local file."""
    with patch(f"{_PREPROCESS}.session_scope", lambda: _yield_session(db_session)):
        _extract_metadata_best_effort(
            mf.storage_path,
            path.suffix,
            str(path.parent),
            int(mf.id),
            str(mf.content_type),
            existing_local_path=str(path),
            task_id=None,
        )
    db_session.refresh(mf)


@pytest.fixture
def captured_hooks():
    clear_hooks()
    captured: list[Any] = []
    register_transcription_complete(captured.append)
    yield captured
    clear_hooks()


# --- #974: group-prefixed ExifTool tags -------------------------------------------


def test_wav_exiftool_duration_and_audio_specs_reach_important_metadata(padded):
    """A WAV's ``Composite:Duration`` and ``RIFF:*`` specs are no longer dropped."""
    raw = extract_media_metadata(str(padded["wav"]))
    assert raw is not None
    assert "Composite:Duration" in raw, "precondition: PyExifTool emits group-prefixed keys"

    important = get_important_metadata(raw)

    assert important.get("Duration") == pytest.approx(PADDED_AUDIO_S, abs=0.01)
    assert important.get("AudioChannels") == 1
    assert important.get("AudioSampleRate") == 16000
    assert important.get("AudioBitsPerSample") == 16


def test_flac_channels_and_rate_fill_from_their_own_group(padded):
    raw = extract_media_metadata(str(padded["flac"]))
    assert raw is not None

    important = get_important_metadata(raw)

    assert important.get("Duration") == pytest.approx(PADDED_AUDIO_S, abs=0.01)
    assert important.get("AudioChannels") == 1
    assert important.get("AudioSampleRate") == 16000


def test_mp3_raw_bitfield_sample_rate_is_never_read_as_a_rate(padded):
    """``MPEG:SampleRate`` under ``-n`` is a table index (2), not a rate in Hz."""
    raw = extract_media_metadata(str(padded["mp3"]))
    assert raw is not None
    assert raw.get("MPEG:SampleRate") == 2, "precondition: the raw bitfield is present"

    important = get_important_metadata(raw)

    assert "AudioSampleRate" not in important


def test_group_insensitive_fill_never_touches_dates():
    """Recorded-date provenance keys on the group, so dates stay exact-match only."""
    important = get_important_metadata(
        {"Composite:Duration": 12.0, "Vorbis:Date": "2020:01:02", "ID3:ModifyDate": "2021:01:01"}
    )

    assert important["Duration"] == 12.0
    assert "CreateDate" not in important
    assert "ModifyDate" not in important


def test_probe_media_duration_reads_local_files(padded, tmp_path):
    """ffprobe answers where ExifTool cannot (Opus) or only estimates (MP3)."""
    probe = metadata_extractor.probe_media_duration

    assert probe(str(padded["ogg"])) == pytest.approx(PADDED_AUDIO_S, abs=0.05)
    assert probe(str(padded["mp3"])) == pytest.approx(PADDED_AUDIO_S, abs=0.15)
    assert probe(str(tmp_path / "missing.wav")) is None
    assert probe("") is None


@pytest.mark.parametrize(
    ("kind", "content_type", "expected", "tolerance"),
    [
        ("wav", "audio/wav", PADDED_AUDIO_S, 0.01),
        ("ogg", "audio/ogg", PADDED_AUDIO_S, 0.05),
        ("mp3", "audio/mpeg", PADDED_AUDIO_S, 0.15),
        ("flac", "audio/flac", PADDED_AUDIO_S, 0.01),
        ("mp4", "video/mp4", PADDED_VIDEO_S, 0.1),
    ],
)
def test_preprocess_stores_the_container_duration(
    db_session, media_row, padded, kind, content_type, expected, tolerance
):
    """The real preprocess metadata step writes the true media length for every container."""
    mf = media_row(f"padded.{kind}", content_type)

    _run_preprocess_metadata(db_session, mf, padded[kind])

    assert mf.duration == pytest.approx(expected, abs=tolerance)


def test_preprocess_prefers_ffprobe_over_the_exiftool_mp3_estimate(db_session, media_row, padded):
    raw = extract_media_metadata(str(padded["mp3"]))
    assert raw is not None
    assert raw["Composite:Duration"] < EXIFTOOL_MP3_ESTIMATE_CEILING, (
        "precondition: ExifTool under-estimates this MP3"
    )
    mf = media_row("padded.mp3", "audio/mpeg")

    _run_preprocess_metadata(db_session, mf, padded["mp3"])

    assert mf.duration == pytest.approx(PADDED_AUDIO_S, abs=0.15)
    assert mf.metadata_important["Duration"] == pytest.approx(mf.duration)


# --- #969: completion must not overwrite the media duration -------------------------


@pytest.mark.parametrize(
    ("kind", "content_type", "expected"),
    [("wav", "audio/wav", PADDED_AUDIO_S), ("mp4", "video/mp4", PADDED_VIDEO_S)],
)
def test_completion_hook_receives_the_media_duration_not_the_speech_extent(
    db_session, media_row, padded, transcript_segments, captured_hooks, kind, content_type, expected
):
    """Preprocess -> completion -> hook: trailing silence is still counted."""
    # The speech that fits in the unpadded source clip.
    segments = [s for s in transcript_segments if s["end"] <= expected - PAD_S]
    assert segments, "precondition: some speech falls inside the source clip"
    speech_extent = max(s["end"] for s in segments)
    assert speech_extent < expected - 1.0, "precondition: the recording ends in silence"
    mf = media_row(f"padded.{kind}", content_type)
    _run_preprocess_metadata(db_session, mf, padded[kind])
    probed = float(mf.duration)

    update_media_file_transcription_status(db_session, int(mf.id), segments)
    db_session.refresh(mf)
    _fire_completion_metering(db_session, int(mf.id), run_id="run-969", provider="local")

    assert mf.status == FileStatus.COMPLETED
    assert mf.duration == pytest.approx(probed), "completion overwrote the media duration"
    assert len(captured_hooks) == 1
    assert captured_hooks[0].audio_duration_s == pytest.approx(expected, abs=0.1)
    assert captured_hooks[0].run_id == "run-969"


def test_speech_extent_is_only_a_last_resort_when_no_duration_is_known(
    db_session, media_row, transcript_segments, captured_hooks
):
    mf = media_row("unprobed.wav", "audio/wav")
    assert mf.duration is None

    update_media_file_transcription_status(db_session, int(mf.id), transcript_segments)
    db_session.refresh(mf)
    _fire_completion_metering(db_session, int(mf.id), run_id="run-fallback", provider="local")

    assert mf.duration == pytest.approx(max(s["end"] for s in transcript_segments))
    assert captured_hooks[0].audio_duration_s == pytest.approx(mf.duration)
