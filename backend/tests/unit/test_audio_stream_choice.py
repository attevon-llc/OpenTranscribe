"""Every audio decode path must read the SAME track of a multi-track file (issue #1263).

Track 0 is a mono 440 Hz sine, track 1 a stereo 880 Hz sine. With no ``-map`` ffmpeg picks the
stream with the most channels (track 1), while PyAV/faster-whisper reads the first stream
(track 0), so speakers were matched on audio that was never transcribed. The dominant frequency
of what each path decodes tells the tracks apart. Real ffmpeg/PyAV; only the models are absent.
"""

import ast
import pathlib
import shutil
import subprocess

import numpy as np
import pytest

from app.services.playback_rendition import MediaProbe
from app.utils.audio_stream import AUDIO_MAP_SPEC
from app.utils.audio_stream import audio_map_args

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SR = 16000
_PROBE = MediaProbe(
    formats=frozenset({"matroska"}),
    audio_codec="pcm_s16le",
    video_codec=None,
    channels=1,
    sample_rate=SR,
    duration=3.0,
)
APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def _dominant_hz(audio: np.ndarray, sr: int = SR) -> float:
    spec = np.abs(np.fft.rfft(audio[: sr * 2]))
    return float(np.argmax(spec) * sr / len(audio[: sr * 2]))


@pytest.fixture(scope="module")
def two_track(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("two_track") / "two_track.mkv")
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=16000",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=3:sample_rate=16000",
            "-filter_complex", "[1:a]pan=stereo|c0=c0|c1=c0[st]",
            "-map", "0:a", "-map", "[st]", "-c:a", "pcm_s16le",
            "-disposition:a:0", "0", "-disposition:a:1", "default", path,
        ],
        check=True,
        timeout=60,
    )  # fmt: skip
    return path


def _pcm_hz(path: str) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-map", "0:a:0", "-f", "f32le", "-ac", "1",
         "-ar", str(SR), "-"],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    return _dominant_hz(np.frombuffer(out, dtype=np.float32))


def test_fixture_really_has_two_differing_tracks(two_track):
    """Control: without -map ffmpeg really picks the other (stereo) track."""
    out = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", two_track, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    assert _dominant_hz(np.frombuffer(out, dtype=np.float32)) == pytest.approx(880, abs=5)
    assert _pcm_hz(two_track) == pytest.approx(440, abs=5)


def test_transcription_loader_reads_first_track(two_track):
    from app.transcription.audio import load_audio

    assert _dominant_hz(load_audio(two_track)) == pytest.approx(440, abs=5)


def test_speaker_embedding_loader_reads_first_track(two_track):
    from app.services.speaker_embedding_service import SpeakerEmbeddingService

    wav, sr = SpeakerEmbeddingService._load_audio(two_track, SR)
    assert _dominant_hz(wav[0].numpy(), sr) == pytest.approx(440, abs=5)


def test_segment_and_full_loaders_read_first_track(two_track):
    from app.services.audio_segment_utils import extract_audio_segment_np
    from app.services.audio_segment_utils import load_full_audio_np

    seg = extract_audio_segment_np(two_track, 0.0, 2.0, SR)
    assert seg is not None
    assert _dominant_hz(seg) == pytest.approx(440, abs=5)
    assert _dominant_hz(load_full_audio_np(two_track, SR)) == pytest.approx(440, abs=5)


def test_wav_preparation_reads_first_track(two_track, tmp_path):
    from app.tasks.transcription.audio_processor import convert_audio_format
    from app.tasks.transcription.audio_processor import extract_audio_from_video
    from app.transcription.audio import load_audio

    for fn in (extract_audio_from_video, convert_audio_format):
        out = str(tmp_path / f"{fn.__name__}.wav")
        fn(two_track, out)
        assert _dominant_hz(load_audio(out)) == pytest.approx(440, abs=5)


def test_waveform_generator_reads_first_track(two_track):
    from app.tasks.transcription.waveform_generator import WaveformGenerator

    gen = WaveformGenerator()
    raw = gen._extract_raw_audio(two_track, 3.0)
    assert raw is not None
    assert _dominant_hz(raw, gen.WAVEFORM_SAMPLE_RATE) == pytest.approx(440, abs=5)


def test_playback_rendition_reads_first_track(two_track, tmp_path):
    from app.services.playback_rendition import build_encode_command

    cmd = build_encode_command(two_track, str(tmp_path / "r.m4a"), _PROBE)
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    assert _pcm_hz(str(tmp_path / "r.m4a")) == pytest.approx(440, abs=8)


def test_audio_extraction_command_reads_first_track(two_track, tmp_path):
    from app.services.audio_extraction import _build_audio_extract_command

    out = str(tmp_path / "x.wav")
    cmd = _build_audio_extract_command("ffmpeg", two_track, out, ["-c:a", "pcm_s16le"])
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    assert _pcm_hz(out) == pytest.approx(440, abs=5)


def test_pyav_default_index_matches_helper():
    """faster-whisper hard-codes ``decode(audio=0)``; fail loudly if an upgrade changes that."""
    import inspect

    import faster_whisper.audio as fw

    from app.utils.audio_stream import AUDIO_STREAM_INDEX

    assert f"audio={AUDIO_STREAM_INDEX})" in inspect.getsource(fw.decode_audio)
    assert audio_map_args() == ["-map", AUDIO_MAP_SPEC]


# --- structural guard: no ffmpeg audio call without the shared selector --------------------

GUARDED = [
    "transcription/audio.py",
    "tasks/transcription/audio_processor.py",
    "tasks/transcription/waveform_generator.py",
    "services/speaker_embedding_service.py",
    "services/playback_rendition.py",
    "services/audio_segment_utils.py",
    "services/audio_extraction.py",
]
_SELECTORS = {"audio_map_args", "AUDIO_MAP_SPEC", "audio_map_spec"}


def unselected_ffmpeg_functions(source: str) -> list[str]:
    """Names of functions that build/run ffmpeg audio decode without the shared selector."""
    bad = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # Own body only: a nested def is judged on its own.
        nodes = [n for stmt in fn.body for n in ast.walk(stmt)]
        uses_ffmpeg = any(
            (isinstance(n, ast.List) and any(isinstance(e, ast.Constant) and e.value == "-i" for e in n.elts))
            or (isinstance(n, ast.Attribute) and n.attr == "input"
                and isinstance(n.value, ast.Name) and n.value.id == "ffmpeg")
            for n in nodes
        )  # fmt: skip
        uses_selector = any(
            (isinstance(n, ast.Name) and n.id in _SELECTORS)
            or (isinstance(n, ast.Attribute) and n.attr in _SELECTORS)
            for n in nodes
        )
        if uses_ffmpeg and not uses_selector:
            bad.append(fn.name)
    return bad


def test_guard_must_fire_on_unselected_call():
    src = 'def f(p):\n    return ["ffmpeg", "-i", p, "-f", "f32le", "-"]\n'
    assert unselected_ffmpeg_functions(src) == ["f"]
    py = "import ffmpeg\ndef g(p, o):\n    ffmpeg.input(p).output(o).run()\n"
    assert unselected_ffmpeg_functions(py) == ["g"]


def test_guard_must_stay_clean_with_selector():
    src = 'def f(p):\n    return ["ffmpeg", "-i", p, *audio_map_args(), "-"]\n'
    assert unselected_ffmpeg_functions(src) == []
    py = "import ffmpeg\ndef g(p, o):\n    ffmpeg.input(p).output(o, map=AUDIO_MAP_SPEC).run()\n"
    assert unselected_ffmpeg_functions(py) == []


@pytest.mark.parametrize("rel", GUARDED)
def test_decode_modules_always_select_the_shared_stream(rel):
    assert unselected_ffmpeg_functions((APP / rel).read_text()) == []
