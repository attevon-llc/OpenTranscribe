"""Upload content sniffing: multi-byte container signatures, declared-type and extension agreement."""

import struct

import pytest

from app.utils.file_validation import validate_uploaded_file


def _ftyp(brand: bytes = b"isom") -> bytes:
    return (
        struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x02\x00" + b"isomiso2" + b"\x00" * 40
    )


def _riff(form: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 1000) + form + b"fmt " + b"\x00" * 40


def _ts() -> bytes:
    return (b"\x47\x40\x00\x10" + b"\x00" * 184) * 3


# Real formats that must keep passing: (header bytes, declared type, filename)
VALID = [
    (_ftyp(), "video/mp4", "clip.mp4"),
    (_ftyp(b"M4A "), "audio/mp4", "memo.m4a"),
    (_ftyp(b"M4A "), "audio/x-m4a", "memo.m4a"),
    (_ftyp(b"qt  "), "video/quicktime", "clip.mov"),
    (_ftyp(b"3gp4"), "video/3gpp", "clip.3gp"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 60, "video/webm", "clip.webm"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 60, "audio/webm;codecs=opus", "rec.webm"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 60, "video/x-matroska", "clip.mkv"),
    (b"OggS" + b"\x00" * 60, "audio/ogg", "a.ogg"),
    (b"OggS" + b"\x00" * 60, "audio/opus", "a.opus"),
    (_riff(b"WAVE"), "audio/wav", "a.wav"),
    (_riff(b"WAVE"), "audio/x-wav", "a.wav"),
    (_riff(b"WAVE"), "audio/vnd.wave", "a.wav"),
    (_riff(b"AVI "), "video/x-msvideo", "a.avi"),
    (b"ID3\x04\x00" + b"\x00" * 60, "audio/mpeg", "a.mp3"),
    (b"\xff\xfb\x90\x00" + b"\x00" * 60, "audio/mpeg", "a.mp3"),
    (b"\xff\xfd\x90\x00" + b"\x00" * 60, "audio/mpeg", "a.mp2"),
    (b"\xff\xf1\x50\x80" + b"\x00" * 60, "audio/aac", "a.aac"),
    (b"fLaC" + b"\x00" * 60, "audio/flac", "a.flac"),
    (b"FORM\x00\x00\x00\x10AIFF" + b"\x00" * 50, "audio/aiff", "a.aiff"),
    (b"\x30\x26\xb2\x75\x8e\x66\xcf\x11" + b"\x00" * 56, "audio/x-ms-wma", "a.wma"),
    (_ts() + b"\x00" * 0, "video/mp2t", "a.ts"),
    (b"\x00\x00\x01\xba" + b"\x00" * 60, "video/mpeg", "a.mpg"),
    (b"\x0b\x77" + b"\x00" * 60, "audio/ac3", "a.ac3"),
    (b"FLV\x01" + b"\x00" * 60, "video/x-flv", "a.flv"),
    (b"#!AMR\n" + b"\x00" * 60, "audio/amr", "a.amr"),
    # No/unknown extension cannot contradict the content.
    (_riff(b"WAVE"), "audio/wav", "recording"),
    (_riff(b"WAVE"), "audio/wav", None),
]


@pytest.mark.parametrize(("header", "mime", "name"), VALID)
def test_valid_media_still_passes(header, mime, name):
    ok, detail = validate_uploaded_file(header.ljust(256, b"\x00"), mime, name)
    assert ok is True, detail


def test_webp_riff_is_not_accepted_as_wav():
    ok, _ = validate_uploaded_file(_riff(b"WEBP"), "audio/wav", "a.wav")
    assert not ok


def test_unknown_riff_form_rejected():
    ok, _ = validate_uploaded_file(_riff(b"XXXX"), "audio/wav", "a.wav")
    assert not ok


def test_truncated_riff_rejected():
    ok, _ = validate_uploaded_file(b"RIFF\x00\x00\x00\x00", "audio/wav", "a.wav")
    assert not ok


def test_single_byte_ts_prefix_rejected():
    ok, _ = validate_uploaded_file(b"G" + b"\x01" * 100, "video/mp2t", "a.ts")
    assert not ok


def test_text_starting_with_g_declared_mp2t_rejected():
    ok, _ = validate_uploaded_file(b"GIF89a....not media at all" * 10, "video/mp2t", "a.ts")
    assert not ok


def test_declared_type_must_match_container():
    ok, _ = validate_uploaded_file(_riff(b"WAVE"), "video/mp4", "a.wav")
    assert not ok
    ok, _ = validate_uploaded_file(_ftyp(), "audio/wav", "a.mp4")
    assert not ok


def test_same_category_wrong_container_rejected():
    # Previously accepted as a "browser quirk": audio/* declared, video container found.
    ok, _ = validate_uploaded_file(b"fLaC" + b"\x00" * 60, "audio/mpeg", "a.mp3")
    assert not ok


def test_extension_must_match_container():
    ok, _ = validate_uploaded_file(_riff(b"WAVE"), "audio/wav", "evil.mp4")
    assert not ok
    ok, _ = validate_uploaded_file(_ftyp(), "video/mp4", "evil.wav")
    assert not ok


def test_extension_case_insensitive():
    ok, detail = validate_uploaded_file(_riff(b"WAVE"), "audio/wav", "A.WAV")
    assert ok is True, detail


def test_heic_image_brand_rejected_as_video():
    ok, _ = validate_uploaded_file(_ftyp(b"heic"), "video/mp4", "a.mp4")
    assert not ok


def test_non_media_declared_type_rejected():
    ok, _ = validate_uploaded_file(_riff(b"WAVE"), "image/png", "a.png")
    assert not ok


@pytest.mark.parametrize(
    ("fixture", "mime"),
    [("sample_short.wav", "audio/wav"), ("sample_short.mp4", "video/mp4")],
)
def test_committed_real_media_fixtures_pass(fixture, mime):
    from pathlib import Path

    from app.utils.file_validation import HEADER_READ_SIZE

    path = Path(__file__).resolve().parents[1] / "fixtures" / "media" / fixture
    with open(path, "rb") as fp:
        assert validate_uploaded_file(fp, mime, fixture)[0] is True
    head = path.read_bytes()[:HEADER_READ_SIZE]
    assert validate_uploaded_file(head, mime, fixture)[0] is True
