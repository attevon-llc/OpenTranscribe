"""Characterization tests for ``app/tasks/transcription/metadata_extractor.py``.

This module turns raw ExifTool/ffprobe output into the fields the API and chat's
recorded-date feature treat as ground truth (``MediaFile.creation_date``,
``AudioFormat``, etc.). It has real defects, and the point of these tests is to pin
what it does TODAY — including the wrong parts — so a future fix has to touch the test,
not silently drift the behaviour underneath the fix.

What is pinned here, in order:

1. **``_parse_media_date`` raises, it does not return ``None``, on a genuinely
   unparsable string** — contradicting its own docstring ("Returns: ... or None if
   parsing fails"). ``None`` is reserved for falsy input and the small
   ``_INVALID_DATE_PATTERNS`` set; every other unparsable string falls through both
   parser stages and hits the explicit ``raise ValueError`` at the end of the
   function. This is an open defect in the docstring, not the tested test — see
   ``test_parse_media_date_raises_on_garbage_input_despite_docstring_promising_none``.
2. **``_parse_quicktime_format`` blindly assumes a ``YYYY:MM:DD HH:MM:SS`` layout.**
   ``date_str.replace(":", "-", 2)`` replaces whichever two colons occur first,
   assuming they belong to the date portion. Fed a string whose date portion
   already uses ``-`` (only the time portion has colons), it corrupts the TIME
   instead, producing something ``datetime.fromisoformat`` cannot parse, so the
   function returns ``None`` for input that read like a real date.
3. **``get_important_metadata``'s ``CreateDate`` field mapping has an exact priority
   order** — YouTube's ``QuickTime:ContentCreateDate`` first, generic keys later.
   Pinned so a reordering (which would silently change which timestamp product
   features treat as "the" creation date) shows up as a test failure.
4. **``update_media_file_metadata`` leaves ``creation_date`` as ``None`` when no date
   field is present anywhere in the container metadata.** The function's own comment
   block (L539-L552) explains this is deliberate — the filesystem-mtime and
   ``upload_time`` fallbacks were removed on purpose so ``NULL`` means "the container
   did not say", not "we guessed". Pinned here so that guarantee cannot silently
   regress by someone adding a fallback back in.
5. **``_try_parse_creation_date_from_fields`` falls through to the next candidate
   field on a ``ValueError``** rather than aborting the whole scan. A malformed
   ``CreateDate`` must not prevent a valid ``DateTimeOriginal`` later in the
   candidate list from being used.
6. **Open defect in ``_map_ffprobe_format``/``_map_ffprobe_audio_stream``:
   ``AudioFormat`` is set from the CONTAINER's ``format_long_name`` (e.g. "QuickTime
   / MOV") before the audio stream's actual codec name is considered.** The audio
   stream mapper's guard, ``if not out.get("AudioFormat")``, is checked only after
   the format mapper already populated the key with the container string via
   ``setdefault`` — so in the realistic order these two are always called in
   (``extract_media_metadata_from_url``: format first, then streams), the guard is
   always false and the real codec name (e.g. "aac") never overwrites the container
   long-name. ``test_map_ffprobe_audio_format_is_shadowed_by_container_long_name``
   asserts today's WRONG output on purpose. Once fixed, ``AudioFormat`` should be
   the audio stream's ``codec_name`` when a stream is present, not the container's
   ``format_long_name``.

These are pure-function/pure-dict tests — no subprocess, no DB session. A lightweight
stand-in object with plain settable attributes stands in for the SQLAlchemy
``MediaFile`` in items 4 and 5, since ``update_media_file_metadata`` and
``_try_parse_creation_date_from_fields`` only ever read/write attributes on it.

Following the characterization-test convention of ``tests/unit/test_transcription_storage.py``.
"""

from __future__ import annotations

import datetime

import pytest

from app.tasks.transcription.metadata_extractor import _map_ffprobe_audio_stream
from app.tasks.transcription.metadata_extractor import _map_ffprobe_format
from app.tasks.transcription.metadata_extractor import _parse_media_date
from app.tasks.transcription.metadata_extractor import _parse_quicktime_format
from app.tasks.transcription.metadata_extractor import _try_parse_creation_date_from_fields
from app.tasks.transcription.metadata_extractor import get_important_metadata
from app.tasks.transcription.metadata_extractor import update_media_file_metadata


class _FakeMediaFile:
    """Minimal stand-in for ``MediaFile`` exposing only the attrs these functions touch."""

    def __init__(self) -> None:
        self.creation_date = None
        self.last_modified_date = None
        self.resolution_width = None
        self.resolution_height = None
        self.frame_rate = None
        self.codec = None
        self.frame_count = None
        self.aspect_ratio = None
        self.audio_channels = None
        self.audio_sample_rate = None
        self.audio_bit_depth = None
        self.duration = None
        self.device_make = None
        self.device_model = None
        self.title = None
        self.author = None
        self.description = None
        self.metadata_raw = None
        self.metadata_important = None
        self.important_metadata = None
        self.metadata = None
        self.file_size = None
        self.media_format = None


# --- 1. _parse_media_date docstring vs. actual behaviour -------------------------


def test_parse_media_date_raises_on_garbage_input_despite_docstring_promising_none() -> None:
    """Pins the ACTUAL behaviour, which contradicts the function's own docstring.

    The docstring says "Returns: Parsed datetime object or None if parsing fails".
    In reality ``None`` is only returned for falsy input or a small hard-coded set of
    known-invalid patterns (epoch/zero dates). Any other string that both date-pattern
    stages fail to parse hits the explicit ``raise ValueError`` at the end of the
    function instead of returning ``None``. This is the defect being pinned — fixing
    it means either updating the docstring to describe the raise, or changing the
    function to actually return ``None`` on parse failure (and this test must then
    flip to asserting ``is None``).
    """
    with pytest.raises(ValueError, match="Unable to parse date format"):
        _parse_media_date("not-a-real-date-at-all")


def test_parse_media_date_returns_none_for_known_invalid_pattern() -> None:
    """Control: the ONE path that legitimately returns None for a "bad" string."""
    assert _parse_media_date("0000:00:00 00:00:00") is None


def test_parse_media_date_returns_none_for_falsy_input() -> None:
    """Control: empty string short-circuits before any parsing is attempted."""
    assert _parse_media_date("") is None


# --- 2. _parse_quicktime_format's blind colon-replacement -------------------------


def test_parse_quicktime_format_corrupts_time_when_date_already_uses_dashes() -> None:
    """Pins the ACTUAL output for a mixed-separator date the function mishandles.

    ``"2023-12-25 14:30:45"`` already has ``-`` in its date portion and only uses
    ``:`` in the time portion. ``_parse_quicktime_format`` unconditionally does
    ``date_str.replace(":", "-", 2)``, assuming the first two colons belong to a
    ``YYYY:MM:DD`` date — but here they belong to the TIME (``14:30`` -> ``14-30``).
    The corrupted string ``"2023-12-25 14-30:45+00:00"`` is not valid ISO-8601, so
    ``datetime.fromisoformat`` raises and the function returns ``None`` for input that
    is, in fact, a perfectly parseable date. Empirically verified against the current
    source before writing this test.
    """
    assert _parse_quicktime_format("2023-12-25 14:30:45") is None


def test_parse_quicktime_format_succeeds_on_the_layout_it_actually_assumes() -> None:
    """Control: the ``YYYY:MM:DD HH:MM:SS`` layout the replace(...) call assumes works."""
    result = _parse_quicktime_format("2023:12:25 14:30:45")
    assert result == datetime.datetime(2023, 12, 25, 14, 30, 45, tzinfo=datetime.UTC)


# --- 3. get_important_metadata CreateDate field-priority ordering -----------------


def test_create_date_prefers_youtube_content_create_date_over_everything_else() -> None:
    """Pins that QuickTime:ContentCreateDate (index 0 of 19 candidates) wins."""
    metadata = {
        "QuickTime:ContentCreateDate": "2020-01-01T00:00:00",
        "CreateDate": "2021-02-02T00:00:00",
        "DateTimeOriginal": "2022-03-03T00:00:00",
    }
    result = get_important_metadata(metadata)
    assert result["CreateDate"] == "2020-01-01T00:00:00"


def test_create_date_prefers_generic_create_date_over_date_time_original() -> None:
    """Pins that plain ``CreateDate`` (index 2) beats ``DateTimeOriginal`` (index 3)
    when neither of the two higher-priority QuickTime keys is present."""
    metadata = {
        "CreateDate": "2021-02-02T00:00:00",
        "DateTimeOriginal": "2022-03-03T00:00:00",
    }
    result = get_important_metadata(metadata)
    assert result["CreateDate"] == "2021-02-02T00:00:00"


# --- 4. update_media_file_metadata: no date fields -> creation_date stays None ----


def test_update_media_file_metadata_leaves_creation_date_none_with_no_date_fields() -> None:
    """Pins that there is NO filesystem-mtime or upload_time fallback left.

    The production code's comment (metadata_extractor.py L539-L552) says this is
    intentional: a container with no date field must leave ``creation_date`` as
    ``NULL`` rather than substituting a guess, because downstream features treat a
    non-null ``creation_date`` as "the container told us". This test guards against
    that fallback silently being reintroduced.
    """
    media_file = _FakeMediaFile()
    extracted_metadata = {
        "File:MIMEType": "audio/mpeg",
        "File:FileType": "MP3",
        # deliberately no CreateDate / DateTimeOriginal / ModifyDate / any date key
    }
    update_media_file_metadata(
        media_file, extracted_metadata, content_type="audio/mpeg", file_path=""
    )
    assert media_file.creation_date is None
    # Sanity: metadata *was* processed (not an early-return bug masquerading as the pin).
    assert media_file.media_format == "MP3"


# --- 5. _try_parse_creation_date_from_fields falls through on ValueError ----------


def test_bad_create_date_falls_through_to_date_time_original() -> None:
    """A malformed CreateDate must not abort the scan of later candidate fields."""
    media_file = _FakeMediaFile()
    important_metadata = {
        "CreateDate": "not-a-real-date-at-all",  # raises ValueError inside _parse_media_date
        "DateTimeOriginal": "2023:12:25 14:30:45",  # valid, should win
    }
    _try_parse_creation_date_from_fields(media_file, important_metadata)
    assert media_file.creation_date == datetime.datetime(
        2023, 12, 25, 14, 30, 45, tzinfo=datetime.UTC
    )


def test_valid_create_date_wins_without_reaching_later_fields() -> None:
    """Control: when CreateDate parses cleanly, it wins over DateTimeOriginal."""
    media_file = _FakeMediaFile()
    important_metadata = {
        "CreateDate": "2023:12:25 14:30:45",
        "DateTimeOriginal": "2099:01:01 00:00:00",
    }
    _try_parse_creation_date_from_fields(media_file, important_metadata)
    assert media_file.creation_date == datetime.datetime(
        2023, 12, 25, 14, 30, 45, tzinfo=datetime.UTC
    )


# --- 6. _map_ffprobe_format / _map_ffprobe_audio_stream: AudioFormat shadowing ----


def test_map_ffprobe_audio_format_is_shadowed_by_container_long_name() -> None:
    """Open defect: asserts today's WRONG AudioFormat value on purpose.

    Realistic ffprobe output for an MP4/M4A container with an AAC audio stream. In
    the real call order (``extract_media_metadata_from_url``: format mapped first,
    then streams), ``_map_ffprobe_format`` already writes
    ``AudioFormat = "QuickTime / MOV"`` via ``setdefault`` before
    ``_map_ffprobe_audio_stream`` runs. That mapper's own guard
    (``if not out.get("AudioFormat")``) is then always False, so the real audio codec
    name ("aac") never overwrites the container long-name — ``AudioFormat`` ends up
    describing the CONTAINER, not the audio codec. Once fixed, this should assert
    ``out["AudioFormat"] == "aac"``.
    """
    fmt = {
        "duration": "120.5",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
    }
    audio_stream = {
        "codec_type": "audio",
        "channels": 2,
        "sample_rate": "44100",
        "codec_name": "aac",
    }

    out: dict = {}
    _map_ffprobe_format(fmt, out)
    _map_ffprobe_audio_stream(audio_stream, out)

    assert out["AudioFormat"] == "QuickTime / MOV"
    assert out["AudioFormat"] != "aac"


def test_map_ffprobe_audio_format_uses_codec_name_when_format_mapped_second() -> None:
    """Control proving the guard itself works: reverse the call order.

    If the audio-stream mapper runs BEFORE the format mapper, ``AudioFormat`` is
    correctly set from ``codec_name`` and the format mapper's ``setdefault`` then
    leaves it alone. This isolates the defect above to CALL ORDER, not to the guard
    logic being broken outright.
    """
    fmt = {
        "duration": "120.5",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
    }
    audio_stream = {
        "codec_type": "audio",
        "channels": 2,
        "sample_rate": "44100",
        "codec_name": "aac",
    }

    out: dict = {}
    _map_ffprobe_audio_stream(audio_stream, out)
    _map_ffprobe_format(fmt, out)

    assert out["AudioFormat"] == "aac"
