"""Choosing the audio track of a multi-track file (issue #1263): probe, decode, playback.

A real two-track MKV is generated with ffmpeg: track 0 is a mono 440 Hz sine tagged ``eng``,
track 1 a stereo 880 Hz sine tagged ``por`` and titled. The dominant frequency of what a path
decodes says which track it read. Only the models are absent; ffmpeg, ffprobe and PyAV are real.
"""

import json
import shutil
import subprocess

import numpy as np
import pytest

from app.services.playback_rendition import MediaProbe
from app.services.playback_rendition import PlaybackNeed
from app.services.playback_rendition import classify_playback
from app.utils.audio_stream import audio_map_args
from app.utils.audio_stream import audio_streams_from_ffprobe
from app.utils.audio_stream import language_mismatch
from app.utils.audio_stream import transcribed_track
from app.utils.audio_stream import validate_audio_stream_index

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)

SR = 16000
_PROBE = MediaProbe(
    formats=frozenset({"matroska"}),
    audio_codec="pcm_s16le",
    video_codec=None,
    channels=1,
    sample_rate=SR,
    duration=3.0,
)


def _hz(audio: np.ndarray, sr: int = SR) -> float:
    window = audio[: sr * 2]
    return float(np.argmax(np.abs(np.fft.rfft(window))) * sr / len(window))


@pytest.fixture(scope="module")
def two_track(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("tracks") / "two_track.mkv")
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=16000",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=3:sample_rate=16000",
            "-filter_complex", "[1:a]pan=stereo|c0=c0|c1=c0[st]",
            "-map", "0:a", "-map", "[st]", "-c:a", "pcm_s16le",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:1", "language=por", "-metadata:s:a:1", "title=Dublagem",
            "-disposition:a:0", "default", "-disposition:a:1", "0", path,
        ],
        check=True,
        timeout=60,
    )  # fmt: skip
    return path


def _probe_json(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, check=True, timeout=30,
    ).stdout  # fmt: skip
    parsed: dict = json.loads(out)
    return parsed


# --- ffprobe -> the stored track list ----------------------------------------------------------


def test_probe_lists_both_tracks_with_language_title_channels_default(two_track):
    streams = audio_streams_from_ffprobe(_probe_json(two_track))

    assert [s["index"] for s in streams] == [0, 1]
    assert [s["language"] for s in streams] == ["eng", "por"]
    assert [s["channels"] for s in streams] == [1, 2]
    assert streams[1]["title"] == "Dublagem"
    assert streams[0]["title"] is None
    assert [s["default"] for s in streams] == [True, False]
    assert all(s["codec"] == "pcm_s16le" for s in streams)


def test_index_is_the_audio_ordinal_not_the_container_stream_number():
    data = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "tags": {"language": "und"},
            },
            {"codec_type": "subtitle", "codec_name": "mov_text"},
            {
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "tags": {"language": "fra"},
            },
        ]
    }
    streams = audio_streams_from_ffprobe(data)
    assert [(s["index"], s["language"]) for s in streams] == [(0, None), (1, "fra")]


def test_validate_index_against_stored_tracks():
    streams = [{"index": 0}, {"index": 1}]
    assert validate_audio_stream_index(1, streams)
    assert not validate_audio_stream_index(2, streams)
    assert not validate_audio_stream_index(-1, streams)
    assert validate_audio_stream_index(0, None)  # never probed: only the first can be claimed
    assert not validate_audio_stream_index(1, None)


def test_transcribed_track_defaults_to_first():
    streams = [{"index": 0, "language": "eng"}, {"index": 1, "language": "por"}]
    assert transcribed_track(streams, None) == streams[0]
    assert transcribed_track(streams, 1) == streams[1]
    assert transcribed_track(None, 1) is None


# --- the mismatch rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detected", "tag", "expected"),
    [
        ("en", "por", True),  # the bug report: English expected, Portuguese track
        ("pt", "por", False),  # 639-1 vs 639-2 agree
        ("pt", "pt-BR", False),
        ("PT", "por", False),
        ("fr", "fra", False),
        ("fr", "fre", False),  # bibliographic spelling
        (None, "por", False),  # nothing detected: nothing to compare
        ("en", None, False),  # untagged track
        ("auto", "por", False),  # a detector's decline is not a language
    ],
)
def test_language_mismatch(detected, tag, expected):
    assert language_mismatch(detected, tag) is expected


# --- decode paths follow the chosen track ------------------------------------------------------


def test_map_args_name_the_chosen_audio_stream():
    assert audio_map_args() == ["-map", "0:a:0?"]
    assert audio_map_args(1) == ["-map", "0:a:1?"]


def test_wav_preparation_reads_the_chosen_track(two_track, tmp_path):
    from app.tasks.transcription.audio_processor import convert_audio_format
    from app.tasks.transcription.audio_processor import extract_audio_from_video
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription
    from app.transcription.audio import load_audio

    for fn in (extract_audio_from_video, convert_audio_format):
        out = str(tmp_path / f"{fn.__name__}.wav")
        fn(two_track, out, audio_stream_index=1)
        assert _hz(load_audio(out)) == pytest.approx(880, abs=5)
    prepared = prepare_audio_for_transcription(
        two_track, "video/x-matroska", str(tmp_path), audio_stream_index=1
    )
    assert _hz(load_audio(prepared)) == pytest.approx(880, abs=5)
    default = prepare_audio_for_transcription(two_track, "video/x-matroska", str(tmp_path))
    assert _hz(load_audio(default)) == pytest.approx(440, abs=5)


def test_segment_and_full_loaders_read_the_chosen_track(two_track):
    from app.services.audio_segment_utils import extract_audio_segment_np
    from app.services.audio_segment_utils import load_full_audio_np

    seg = extract_audio_segment_np(two_track, 0.0, 2.0, SR, audio_stream_index=1)
    assert seg is not None
    assert _hz(seg) == pytest.approx(880, abs=5)
    assert _hz(load_full_audio_np(two_track, SR, audio_stream_index=1)) == pytest.approx(880, abs=5)


def test_waveform_generator_reads_the_chosen_track(two_track):
    from app.tasks.transcription.waveform_generator import WaveformGenerator

    gen = WaveformGenerator(audio_stream_index=1)
    raw = gen._extract_raw_audio(two_track, 3.0)
    assert raw is not None
    assert _hz(raw, gen.WAVEFORM_SAMPLE_RATE) == pytest.approx(880, abs=5)


def test_audio_download_command_reads_the_chosen_track(two_track, tmp_path):
    from app.services.audio_extraction import _build_audio_extract_command

    out = str(tmp_path / "dl.wav")
    cmd = _build_audio_extract_command("ffmpeg", two_track, out, ["-c:a", "pcm_s16le"], 1)
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    decoded = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", out, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    assert _hz(np.frombuffer(decoded, dtype=np.float32)) == pytest.approx(880, abs=5)


def test_playback_rendition_carries_the_chosen_track(two_track, tmp_path):
    from app.services.playback_rendition import build_encode_command

    out = str(tmp_path / "r.m4a")
    subprocess.run(
        build_encode_command(two_track, out, _PROBE, 1), check=True, capture_output=True, timeout=60
    )
    decoded = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", out, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    assert _hz(np.frombuffer(decoded, dtype=np.float32)) == pytest.approx(880, abs=8)


def test_a_non_first_track_always_needs_a_rendition_a_browser_cannot_pick_it():
    playable_video = MediaProbe(
        formats=frozenset({"mp4"}), audio_codec="aac", video_codec="h264", channels=2
    )
    playable_audio = MediaProbe(formats=frozenset({"mp3"}), audio_codec="mp3", video_codec=None)
    assert classify_playback(playable_video) is PlaybackNeed.NONE
    assert classify_playback(playable_video, 0) is PlaybackNeed.NONE
    assert classify_playback(playable_video, 1) is PlaybackNeed.AUDIO_ONLY_PREVIEW
    assert classify_playback(playable_audio, 1) is PlaybackNeed.CONVERT_AUDIO
