"""The local Whisper catalog is the single source for every surface that lists models (#1246).

The settings list, the provider catalog, the per-file validator, the ``/local-model/set``
allow-list and ``scripts/download-models.py`` all read ``core/local_whisper_catalog``. These tests
pin that they agree with it and that the catalog agrees with faster-whisper's own tables.
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.core import local_whisper_catalog as catalog
from app.schemas.media import VALID_LOCAL_WHISPER_MODELS
from app.services.asr.factory import ASRProviderFactory

EXPECTED_IDS = {
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "medium",
    "medium.en",
    "large-v1",
    "large-v2",
    "large-v3",
    "large-v3-turbo",
    "nyrahealth/faster_CrisperWhisper",
}


def test_catalog_holds_exactly_the_thirteen_owner_listed_models():
    assert {m.id for m in catalog.LOCAL_WHISPER_MODELS} == EXPECTED_IDS
    assert len(catalog.LOCAL_WHISPER_MODELS) == 13


def test_per_file_validator_set_is_the_catalog():
    assert VALID_LOCAL_WHISPER_MODELS == catalog.LOCAL_WHISPER_MODEL_IDS


def test_every_entry_is_complete():
    assert catalog.LOCAL_WHISPER_MODELS
    for m in catalog.LOCAL_WHISPER_MODELS:
        assert "/" in m.repo_id, m.id
        assert m.approx_bytes > 0, m.id
        assert m.vram_hint_gb > 0, m.id
        assert m.display_name, m.id
        assert m.language_support in {"multilingual", "english_only", "english_optimized"}


def test_exactly_one_default_and_it_is_turbo():
    defaults = [m.id for m in catalog.LOCAL_WHISPER_MODELS if m.is_default]
    assert defaults == ["large-v3-turbo"]


def test_short_ids_map_to_the_repo_faster_whisper_uses():
    fw_utils = pytest.importorskip("faster_whisper.utils")
    checked = 0
    for m in catalog.LOCAL_WHISPER_MODELS:
        if "/" in m.id:
            continue
        assert fw_utils._MODELS[m.id] == m.repo_id, m.id
        checked += 1
    assert checked == 12


def test_allow_patterns_equal_faster_whispers_download_patterns():
    fw_utils = pytest.importorskip("faster_whisper.utils")
    source = inspect.getsource(fw_utils.download_model)
    match = re.search(r"allow_patterns\s*=\s*\[(.*?)\]", source, re.S)
    assert match, "faster_whisper.utils.download_model no longer lists allow_patterns"
    in_fw = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert in_fw == catalog.ALLOW_PATTERNS


def test_provider_catalog_local_models_are_the_catalog_in_order():
    local = ASRProviderFactory.get_provider_catalog()["local"]
    assert [m["id"] for m in local["models"]] == [m.id for m in catalog.LOCAL_WHISPER_MODELS]
    for served, entry in zip(local["models"], catalog.LOCAL_WHISPER_MODELS, strict=True):
        assert served["display_name"] == entry.display_name
        assert served["supports_translation"] is entry.supports_translation
        assert served["language_support"] == entry.language_support
        assert served["approx_bytes"] == entry.approx_bytes
        assert served["vram_hint_gb"] == entry.vram_hint_gb
        assert bool(served.get("is_default")) is entry.is_default
        assert served["supports_diarization"] is True
        assert served["price_per_min_batch"] == 0


def test_capabilities_unchanged_for_the_models_the_old_catalog_listed():
    """language_support/translation for the eight pre-existing ids must not drift."""
    expected = {
        "large-v3-turbo": ("english_optimized", False),
        "large-v3": ("multilingual", True),
        "large-v2": ("multilingual", True),
        "medium": ("multilingual", True),
        "small": ("multilingual", True),
        "base": ("multilingual", True),
        "tiny": ("multilingual", True),
        "nyrahealth/faster_CrisperWhisper": ("english_only", False),
    }
    for model_id, (lang, translate) in expected.items():
        entry = catalog.get(model_id)
        assert entry is not None
        assert (entry.language_support, entry.supports_translation) == (lang, translate)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("large-v3", "large-v3"),
        ("tiny.en", "tiny.en"),
        ("crisperwhisper", "nyrahealth/faster_CrisperWhisper"),
        ("faster_crisperwhisper", "nyrahealth/faster_CrisperWhisper"),
        ("nyrahealth/CrisperWhisper", "nyrahealth/faster_CrisperWhisper"),
        ("nyrahealth/faster_CrisperWhisper", "nyrahealth/faster_CrisperWhisper"),
        ("distil-large-v3", None),
        ("turbo", None),
        ("someone/else-ct2", None),
        ("Systran/faster-whisper-tiny", None),
        ("", None),
        ("../etc/passwd", None),
    ],
)
def test_canonical_id(name, expected):
    assert catalog.canonical_id(name) == expected


def test_get_returns_none_for_an_alias_and_an_unknown_id():
    assert catalog.get("crisperwhisper") is None
    assert catalog.get("nope") is None
    assert catalog.get("tiny") is not None
