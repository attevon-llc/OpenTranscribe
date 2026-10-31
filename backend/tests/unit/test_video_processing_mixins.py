"""``VideoProcessingService`` composed of its subtitle-burn and audio-extraction mixins.

The service was split by concern (``video_subtitle_burn`` / ``audio_extraction``); these tests run
the pieces that no other test drove end to end, against real ``ffmpeg``/``ffprobe`` and a
recording stand-in for object storage, so a method that fell out of the composition (or a helper
that lost its caller's module) fails here rather than at the first user download.
"""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from app.services import audio_extraction
from app.services.audio_extraction import AudioExtractionMixin
from app.services.audio_extraction import NoAudioTrackError
from app.services.minio_bucket_admin import MinIOService
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.video_processing_service import VideoProcessingService
from app.services.video_subtitle_burn import VideoSubtitleBurnMixin

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe",
)


def _ffmpeg(*args: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    subprocess.run([ffmpeg, "-v", "error", "-y", *args], check=True, timeout=60)


class _Storage:
    """Records uploads; serves one source file for downloads; ``stat_object`` raises on a miss."""

    def __init__(self, source: Path, cached: dict[str, str] | None = None) -> None:
        self.source = source
        self.cached = cached or {}
        self.uploads: list[dict[str, Any]] = []

    def download_file(self, *, object_name: str, file_path: str, bucket_name: str) -> None:
        shutil.copy(self.source, file_path)

    def upload_file(
        self, *, file_path: str, bucket_name: str, object_name: str, content_type: str
    ) -> None:
        self.uploads.append(
            {
                "key": object_name,
                "bucket": bucket_name,
                "content_type": content_type,
                "size": Path(file_path).stat().st_size,
                "suffix": Path(file_path).suffix,
            }
        )
        self.cached[object_name] = content_type

    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        if key not in self.cached:
            raise FileNotFoundError(key)
        return SimpleNamespace(content_type=self.cached[key])


def _service(storage: _Storage) -> VideoProcessingService:
    svc = VideoProcessingService.__new__(VideoProcessingService)
    svc.minio_service = cast(MinIOService, storage)
    svc.cache_bucket = "cache-bucket"
    return svc


@pytest.fixture
def av_file(tmp_path: Path) -> Path:
    out = tmp_path / "av.mkv"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=1:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture
def silent_file(tmp_path: Path) -> Path:
    out = tmp_path / "silent.mkv"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=10", "-c:v", "mpeg4", str(out))
    return out


def test_service_is_composed_of_both_mixins():
    assert issubclass(VideoProcessingService, VideoSubtitleBurnMixin)
    assert issubclass(VideoProcessingService, AudioExtractionMixin)
    for name in ("extract_audio", "peek_cached_audio", "embed_subtitles_in_video"):
        assert callable(getattr(VideoProcessingService, name))


def test_extract_audio_mp3_uploads_an_encoded_file_then_hits_the_cache(av_file, monkeypatch):
    storage = _Storage(av_file)
    svc = _service(storage)
    monkeypatch.setattr(
        VideoProcessingService, "_media_filename", staticmethod(lambda file_id: "talk.mkv")
    )

    key, ext, content_type = svc.extract_audio(5, "media/talk.mkv", "mp3")

    assert (key, ext, content_type) == ("derived/talk_audio_mp3.mp3", "mp3", "audio/mpeg")
    assert len(storage.uploads) == 1
    assert storage.uploads[0]["key"] == key and storage.uploads[0]["size"] > 0
    assert storage.uploads[0]["suffix"] == ".mp3"

    # Second call is served from the cache: no new upload.
    assert svc.extract_audio(5, "media/talk.mkv", "mp3") == (key, ext, content_type)
    assert len(storage.uploads) == 1


def test_extract_audio_original_stream_copies_aac_into_m4a(av_file, monkeypatch):
    storage = _Storage(av_file)
    svc = _service(storage)
    monkeypatch.setattr(
        VideoProcessingService, "_media_filename", staticmethod(lambda file_id: "talk.mkv")
    )

    key, ext, content_type = svc.extract_audio(5, "media/talk.mkv", "original")

    assert (key, ext, content_type) == ("derived/talk_audio_original", "m4a", "audio/mp4")
    # A cached "original" recovers its extension from the stored content type.
    assert svc.peek_cached_audio("talk.mkv", "original") == (key, "m4a", "audio/mp4")


def test_extract_audio_without_an_audio_stream_raises_no_audio_track(silent_file, monkeypatch):
    storage = _Storage(silent_file)
    svc = _service(storage)
    monkeypatch.setattr(
        VideoProcessingService, "_media_filename", staticmethod(lambda file_id: "silent.mkv")
    )

    with pytest.raises(NoAudioTrackError):
        svc.extract_audio(5, "media/silent.mkv", "original")
    with pytest.raises(NoAudioTrackError):
        svc.extract_audio(5, "media/silent.mkv", "mp3")
    assert storage.uploads == []


def test_peek_cached_audio_reports_a_miss_and_a_preset_hit():
    svc = _service(_Storage(Path("unused"), cached={"derived/talk_audio_wav.wav": "audio/wav"}))

    assert svc.peek_cached_audio("talk.mkv", "mp3") is None
    assert svc.peek_cached_audio("talk.mkv", "WAV") == (
        "derived/talk_audio_wav.wav",
        "wav",
        "audio/wav",
    )
    assert audio_extraction.NoAudioTrackError is NoAudioTrackError


def test_embed_subtitles_runs_ffmpeg_and_uploads_the_burned_file(av_file, monkeypatch):
    storage = _Storage(av_file)
    svc = _service(storage)
    monkeypatch.setattr(
        VideoProcessingService,
        "_generate_subtitle_file",
        lambda self, file_id, path, include_speakers, cfg: path.write_text(
            "1\n00:00:00,000 --> 00:00:00,900\nhello\n", encoding="utf-8"
        ),
    )

    key = svc.embed_subtitles_in_video(
        9,
        av_file,
        output_format="mkv",
        original_filename="talk.mkv",
        redaction_cfg=EffectiveRedactionConfig(enabled=False),
    )

    assert key == "derived/9_talk_with_speakers.mp4"
    assert [(u["key"], u["bucket"], u["content_type"]) for u in storage.uploads] == [
        (key, "cache-bucket", "video/mkv")
    ]
    assert storage.uploads[0]["size"] > 0 and storage.uploads[0]["suffix"] == ".mkv"

    # Already cached: returns the key without a second render.
    assert (
        svc.embed_subtitles_in_video(
            9,
            av_file,
            output_format="mkv",
            original_filename="talk.mkv",
            redaction_cfg=EffectiveRedactionConfig(enabled=False),
        )
        == key
    )
    assert len(storage.uploads) == 1
