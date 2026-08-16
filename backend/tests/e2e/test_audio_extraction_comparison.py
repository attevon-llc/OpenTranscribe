"""E2E comparison: self-compiled FFmpeg.wasm core vs. Mediabunny (issue #473).

Drives a real browser against the Vite dev server and runs BOTH client-side extraction
services (`audioExtractionService.ts`, the shipped path; `mediabunnyExtractionService.ts`, an
evaluated alternative not wired into the app) against the same fixture matrix, to give the
`.legal/` licensing decision empirical backing rather than an assertion from vendor docs.

Requirements:
- Dev environment running: ./opentr.sh start dev (frontend serving the real TS source so
  dynamic `import()` of the two services resolves)
- ffmpeg on the host (media fixtures are generated, never downloaded — same convention as
  test_upload.py)

Run:
    pytest backend/tests/e2e/test_audio_extraction_comparison.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_audio_extraction_comparison.py -v --headed
"""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR
from playwright.sync_api import Page

pytestmark = pytest.mark.upload

# (filename, mime type, video codec args or None for audio-only, audio codec args)
MATRIX = [
    ("sample_aac.mp4", "video/mp4", ["-c:v", "libx264", "-pix_fmt", "yuv420p"], ["-c:a", "aac"]),
    (
        "sample_aac.mov",
        "video/quicktime",
        ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
        ["-c:a", "aac"],
    ),
    (
        "sample_opus.mkv",
        "video/x-matroska",
        ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
        ["-c:a", "libopus"],
    ),
    ("sample_opus.webm", "video/webm", ["-c:v", "libvpx"], ["-c:a", "libopus"]),
    ("sample_pcm.avi", "video/x-msvideo", ["-c:v", "mpeg4"], ["-c:a", "pcm_s16le"]),
    ("sample_aac.flv", "video/x-flv", ["-c:v", "flv"], ["-c:a", "aac"]),
    (
        "sample_aac.ts",
        "video/mp2t",
        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts"],
        ["-c:a", "aac"],
    ),
    ("sample_wmv.wmv", "video/x-ms-wmv", ["-c:v", "wmv2"], ["-c:a", "wmav2"]),
    ("sample_vorbis.ogg", "video/ogg", ["-c:v", "libtheora"], ["-c:a", "libvorbis"]),
]
STANDALONE_AUDIO = [
    ("sample.wav", "audio/wav", []),
    ("sample.mp3", "audio/mpeg", ["-c:a", "libmp3lame"]),
    ("sample.aac", "audio/aac", ["-c:a", "aac"]),
    ("sample.flac", "audio/flac", ["-c:a", "flac"]),
]

# Mediabunny ships no AVI/FLV/WMV demuxer/muxer (only ISOBMFF/mov, Matroska/WebM, Ogg, MP3, WAV,
# ADTS, FLAC, MPEG-TS, HLS) — this is the documented gap from .legal/ that this test proves
# empirically rather than asserting from vendor docs.
MEDIABUNNY_UNSUPPORTED = {"sample_pcm.avi", "sample_aac.flv", "sample_wmv.wmv"}

# wmav2 is not in getAudioExtension's codec map (falls back to 'm4a'), and no FFmpeg build —
# minimal or full — can stream-copy a wmav2 stream into an mp4/ipod-muxer container: this is a
# pre-existing app-level gap unrelated to issue #473's build, out of scope to fix here. What IS
# in scope is that it fails loudly instead of silently producing an empty blob (the bug this
# suite's first real run against a live browser caught: exec()'s exit code was never checked).
FFMPEG_KNOWN_UNMAPPED_CODEC = {"sample_wmv.wmv"}


def _generate(filename: str, video_args: list[str], audio_args: list[str]) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available — cannot generate media fixtures")
    FIXTURES_DIR.mkdir(exist_ok=True)
    out: Path = FIXTURES_DIR / filename
    if out.exists():
        return out
    inputs = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if video_args:
        inputs = [
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
        ]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, *video_args, *audio_args]
    if video_args:
        cmd += ["-shortest"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True, timeout=60)
    return out


def _extract_via(
    page: Page, module_path: str, export_name: str, filename: str, mime: str, data_b64: str
) -> dict:
    """Load a fixture into the page as a File, then run one extraction service against it."""
    result: dict = page.evaluate(
        """async ({ modulePath, exportName, filename, mime, dataB64 }) => {
            const mod = await import(/* @vite-ignore */ modulePath);
            const service = mod[exportName];
            const bytes = Uint8Array.from(atob(dataB64), (c) => c.charCodeAt(0));
            const file = new File([bytes], filename, { type: mime });
            try {
                const result = await service.extractAudio(file);
                return { ok: true, size: result.blob.size, filename: result.filename };
            } catch (err) {
                return { ok: false, error: err instanceof Error ? err.message : String(err) };
            }
        }""",
        {
            "modulePath": module_path,
            "exportName": export_name,
            "filename": filename,
            "mime": mime,
            "dataB64": data_b64,
        },
    )
    return result


@pytest.fixture(params=MATRIX + [(f, m, [], a) for f, m, a in STANDALONE_AUDIO], ids=lambda p: p[0])
def extraction_fixture(request) -> tuple[Path, str]:
    filename, mime, video_args, audio_args = request.param
    return _generate(filename, video_args, audio_args), mime


def test_ffmpeg_extraction_succeeds_on_every_fixture(authenticated_page: Page, extraction_fixture):
    """The self-compiled FFmpeg.wasm core must handle every container/codec in the matrix."""
    path, mime = extraction_fixture
    data_b64 = base64.b64encode(path.read_bytes()).decode()

    result = _extract_via(
        authenticated_page,
        "/src/lib/services/audioExtractionService.ts",
        "audioExtractionService",
        path.name,
        mime,
        data_b64,
    )

    if path.name in FFMPEG_KNOWN_UNMAPPED_CODEC:
        assert not result["ok"], (
            f"{path.name} unexpectedly succeeded — if getAudioExtension's codec map grew "
            "support for this codec, move it out of FFMPEG_KNOWN_UNMAPPED_CODEC."
        )
        assert result["error"], (
            f"{path.name} failed with no error message — silent failure regressed"
        )
    else:
        assert result["ok"], f"FFmpeg extraction failed for {path.name}: {result.get('error')}"
        assert result["size"] > 0, f"FFmpeg extraction produced an empty blob for {path.name}"


def test_mediabunny_extraction_matches_documented_format_support(
    authenticated_page: Page, extraction_fixture
):
    """Mediabunny must succeed on its documented formats and fail on AVI/FLV/WMV — the
    empirical evidence for the coverage gap documented in .legal/ and
    mediabunnyExtractionService.ts's module comment."""
    path, mime = extraction_fixture
    data_b64 = base64.b64encode(path.read_bytes()).decode()

    result = _extract_via(
        authenticated_page,
        "/src/lib/services/mediabunnyExtractionService.ts",
        "mediabunnyExtractionService",
        path.name,
        mime,
        data_b64,
    )

    if path.name in MEDIABUNNY_UNSUPPORTED:
        assert not result["ok"], (
            f"Mediabunny unexpectedly succeeded on {path.name} — the documented AVI/FLV/WMV "
            "gap may have closed; update .legal/ and mediabunnyExtractionService.ts's comment."
        )
    else:
        assert result["ok"], f"Mediabunny extraction failed for {path.name}: {result.get('error')}"
        assert result["size"] > 0, f"Mediabunny extraction produced an empty blob for {path.name}"
