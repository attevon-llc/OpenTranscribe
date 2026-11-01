"""Filename extensions: the helpers and the decisions their callers make (D11).

One table of names, basenames and path-like, run through every extension helper, plus the
decision each caller takes from the result. The decisions are what must not move.
"""

from __future__ import annotations

import pytest

from app.api.endpoints.files.media_delivery import _audio_passthrough
from app.models.media import MediaFile
from app.services.storage_recovery_service import content_type_for
from app.tasks.transcription.audio_processor import get_audio_file_extension
from app.utils.file_validation import validate_magic_bytes
from app.utils.media_types import file_extension

pytestmark = pytest.mark.unit

#: name -> (media_types.file_extension, audio_processor's splitext).
#:
#: `file_extension` replaced three helpers (D11). It IS `file_validation._extension`.
#: `files._audio_source_extension` returned the same text without the dot (its caller now
#: compares ".mp3"/".wav"), and "trailing." gave "" there. Path-like names differed:
#: "dir.v2/song" gave "v2/song" and "x.y\\z" "y\\z"; `storage_recovery._object_extension`
#: gave ".y\\z" for "x.y\\z" (it split on "/" only). None of those moved a caller's decision
#: (the tests below). splitext stays separate: it keeps case and calls ".mp3" extensionless.
TABLE = {
    "song.mp3": (".mp3", ".mp3"),
    "Song.MP3": (".mp3", ".MP3"),
    "archive.tar.gz": (".gz", ".gz"),
    "noext": ("", ""),
    ".mp3": (".mp3", ""),
    "trailing.": (".", "."),
    "": ("", ""),
    "a.b.c.WAV": (".wav", ".WAV"),
    "dir.v2/song": ("", ""),
    "user_1/file.mp3": (".mp3", ".mp3"),
    "C:\\Users\\x\\clip.WAV": (".wav", ".WAV"),
    "x.y\\z": ("", ".y\\z"),
    "folder/.hidden": (".hidden", ""),
}


@pytest.mark.parametrize("name", list(TABLE))
def test_each_helper_on_the_shared_table(name):
    assert (file_extension(name), get_audio_file_extension("", name)) == TABLE[name]


def test_no_name_has_no_extension():
    assert file_extension(None) == ""


@pytest.mark.parametrize(
    ("filename", "mode", "passthrough"),
    [
        ("talk.mp3", "audio_mp3", True),
        ("TALK.MP3", "audio_mp3", True),
        ("talk.wav", "audio_mp3", False),
        ("talk.wav", "audio_wav", True),
        ("talk.mp3/x", "audio_mp3", False),
        ("mp3", "audio_mp3", False),
        ("talk.", "audio_wav", False),
        ("anything", "audio_original", True),
    ],
)
def test_download_passthrough_decision(filename, mode, passthrough):
    db_file = MediaFile(content_type="audio/mpeg", filename=filename)

    assert _audio_passthrough(db_file, mode) is passthrough


@pytest.mark.parametrize(
    ("key", "content_type"),
    [
        ("media/7/a.MP3", "audio/mpeg"),
        ("media/7/a.wav", "audio/wav"),
        ("media/7/a.v2/b", "application/octet-stream"),
        ("media/7/noext", "application/octet-stream"),
        ("media/7/a.mp3\\b", "application/octet-stream"),
    ],
)
def test_recovery_content_type_decision(key, content_type):
    assert content_type_for(key) == content_type


MP3_HEADER = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


@pytest.mark.parametrize(
    ("filename", "valid"),
    [
        ("clip.mp3", True),
        ("CLIP.MP3", True),
        ("clip.mp4", False),
        ("C:\\x\\clip.MP4", False),
        ("clip.mp4\\renamed", True),
        ("noext", True),
        (None, True),
    ],
)
def test_upload_extension_agreement_decision(filename, valid):
    assert validate_magic_bytes(MP3_HEADER, "audio/mpeg", filename)[0] is valid
