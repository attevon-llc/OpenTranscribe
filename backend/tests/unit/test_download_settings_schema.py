"""Tests for ``app/schemas/download_settings.py`` (issue #474).

Pydantic v2 wire contract for user download quality preferences. Zero test
coverage before this file. The load-bearing behavior is
``DownloadSettingsUpdate``'s ``extra="forbid"`` (its docstring explains why: a
typo'd key used to be silently dropped, returning a 200 with no change) and
that the two default-bearing schemas actually mirror
``app/core/constants.py``'s option tables rather than a hand-copied literal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.constants import AUDIO_QUALITY_OPTIONS
from app.core.constants import DEFAULT_AUDIO_ONLY
from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_VIDEO_QUALITY
from app.core.constants import VIDEO_QUALITY_OPTIONS
from app.schemas.download_settings import DownloadSettings
from app.schemas.download_settings import DownloadSettingsUpdate
from app.schemas.download_settings import DownloadSystemDefaults

pytestmark = pytest.mark.unit


# =============================================================================
# DownloadSettings — response schema, coded defaults
# =============================================================================
def test_default_construction_uses_the_coded_defaults():
    settings = DownloadSettings()

    assert settings.video_quality == DEFAULT_VIDEO_QUALITY == "best"
    assert settings.audio_only is DEFAULT_AUDIO_ONLY is False
    assert settings.audio_quality == DEFAULT_AUDIO_QUALITY == "best"


def test_explicit_values_override_the_defaults():
    settings = DownloadSettings(video_quality="1080p", audio_only=True, audio_quality="192")

    assert settings.video_quality == "1080p"
    assert settings.audio_only is True
    assert settings.audio_quality == "192"


def test_wrong_type_for_audio_only_is_rejected():
    with pytest.raises(ValidationError):
        DownloadSettings(audio_only="not-a-bool")


def test_round_trips_through_model_dump():
    settings = DownloadSettings(video_quality="720p", audio_only=True, audio_quality="320")

    dumped = settings.model_dump()

    assert dumped == {
        "video_quality": "720p",
        "audio_only": True,
        "audio_quality": "320",
    }


# =============================================================================
# DownloadSettingsUpdate — request schema, all-optional, extra="forbid"
# =============================================================================
def test_empty_update_leaves_every_field_none():
    update = DownloadSettingsUpdate()

    assert update.video_quality is None
    assert update.audio_only is None
    assert update.audio_quality is None


def test_partial_update_sets_only_the_given_field():
    update = DownloadSettingsUpdate(audio_quality="128")

    assert update.audio_quality == "128"
    assert update.video_quality is None
    assert update.audio_only is None


def test_full_update_sets_every_field():
    update = DownloadSettingsUpdate(video_quality="480p", audio_only=True, audio_quality="best")

    assert update.video_quality == "480p"
    assert update.audio_only is True
    assert update.audio_quality == "best"


def test_unknown_field_is_rejected_not_silently_dropped():
    """The docstring's whole point: before ``extra="forbid"``, a typo'd key like
    ``videoQuality`` was silently discarded, ``update_data`` came back empty, and
    the caller got a 200 with no change applied. A typo must be a validation error."""
    with pytest.raises(ValidationError) as excinfo:
        DownloadSettingsUpdate(videoQuality="1080p")

    errors = excinfo.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_model_dump_exclude_unset_reports_only_the_fields_actually_provided():
    """This is the shape ``update_download_settings`` relies on to know which
    fields the caller actually asked to change vs. left untouched."""
    update = DownloadSettingsUpdate(audio_only=False)

    assert update.model_dump(exclude_unset=True) == {"audio_only": False}


# =============================================================================
# DownloadSystemDefaults — response schema, defaults + option tables
# =============================================================================
def test_default_construction_mirrors_constants_defaults_and_option_tables():
    defaults = DownloadSystemDefaults()

    assert defaults.video_quality == DEFAULT_VIDEO_QUALITY
    assert defaults.audio_only == DEFAULT_AUDIO_ONLY
    assert defaults.audio_quality == DEFAULT_AUDIO_QUALITY
    assert defaults.available_video_qualities == VIDEO_QUALITY_OPTIONS
    assert defaults.available_audio_qualities == AUDIO_QUALITY_OPTIONS


def test_available_video_qualities_contains_the_expected_keys():
    defaults = DownloadSystemDefaults()

    assert set(defaults.available_video_qualities) == {
        "best",
        "2160p",
        "1440p",
        "1080p",
        "720p",
        "480p",
        "360p",
    }
    assert defaults.available_video_qualities["1080p"] == "1080p (Full HD)"


def test_available_audio_qualities_contains_the_expected_keys():
    defaults = DownloadSystemDefaults()

    assert set(defaults.available_audio_qualities) == {"best", "320", "192", "128"}
    assert defaults.available_audio_qualities["320"] == "320 kbps"


def test_can_override_the_option_tables_explicitly():
    defaults = DownloadSystemDefaults(
        available_video_qualities={"best": "Best"}, available_audio_qualities={"best": "Best"}
    )

    assert defaults.available_video_qualities == {"best": "Best"}
    assert defaults.available_audio_qualities == {"best": "Best"}
