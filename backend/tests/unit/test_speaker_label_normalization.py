"""Unit tests for shared speaker-label normalization (issue #299).

Network-free. Covers the module-level `normalize_speaker_label` helper and — critically —
its reachability from *both* provider hierarchies. The diarization side previously proxied
through `object.__new__(ASRProvider)`, which raises `TypeError` because `object.__new__`
performs the ABC abstract-method check; every diarization call therefore blew up at runtime
with nothing covering it.
"""

from __future__ import annotations

import pytest

from app.services.asr.base import normalize_speaker_label
from app.services.asr.base import sanitize_provider_error
from app.services.diarization.base import DiarizationProvider
from app.services.diarization.local_provider import LocalDiarizationProvider
from app.services.diarization.pyannote_provider import PyAnnoteCloudDiarizationProvider


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Already canonical — pass through untouched.
        ("SPEAKER_00", "SPEAKER_00"),
        ("SPEAKER_10", "SPEAKER_10"),
        # SPEAKER_-prefixed but unpadded / lowercase — re-padded to the canonical width.
        ("SPEAKER_1", "SPEAKER_01"),
        ("speaker_0", "SPEAKER_00"),  # Gladia, 0-indexed
        ("speaker_7", "SPEAKER_07"),
        # Non-numeric suffix is preserved (only upper-cased).
        ("SPEAKER_UNKNOWN", "SPEAKER_UNKNOWN"),
        # Integer strings — 0-indexed (Deepgram).
        ("0", "SPEAKER_00"),
        ("3", "SPEAKER_03"),
        (5, "SPEAKER_05"),
        # Single letters — 0-indexed (AssemblyAI).
        ("A", "SPEAKER_00"),
        ("C", "SPEAKER_02"),
        # "S#" — 1-indexed (Speechmatics).
        ("S1", "SPEAKER_00"),
        ("S4", "SPEAKER_03"),
        # "speaker N" — 1-indexed.
        ("speaker 1", "SPEAKER_00"),
        ("speaker 2", "SPEAKER_01"),
        # "spk_N" — 0-indexed (AWS Transcribe).
        ("spk_0", "SPEAKER_00"),
        ("spk_3", "SPEAKER_03"),
        # "Guest-N" — 1-indexed (Azure ConversationTranscriber).
        ("Guest-1", "SPEAKER_00"),
        ("Guest-2", "SPEAKER_01"),
        # None passes through.
        (None, None),
    ],
)
def test_normalize_speaker_label_formats(raw, expected):
    assert normalize_speaker_label(raw) == expected


def test_unknown_label_hashes_into_two_digit_range():
    out = normalize_speaker_label("some-vendor-specific-label")
    assert out is not None
    assert out.startswith("SPEAKER_")
    assert out[len("SPEAKER_") :].isdigit()
    assert len(out) == len("SPEAKER_XX")


def test_normalization_is_idempotent():
    """Re-normalizing an already-normalized label must be a no-op."""
    for raw in ("S1", "A", "0", "speaker 1", "spk_3", "Guest-2", "SPEAKER_1"):
        once = normalize_speaker_label(raw)
        assert normalize_speaker_label(once) == once


# ── Reachability from both hierarchies (the actual #299 regression) ──────────────


def test_diarization_provider_normalize_does_not_raise():
    """Regression: the old `object.__new__(ASRProvider)` proxy raised TypeError here."""

    class _Stub(DiarizationProvider):
        def diarize(self, audio_path, config, progress_callback=None):  # pragma: no cover
            raise NotImplementedError

        def supports_speaker_count(self) -> bool:
            return False

        @property
        def provider_name(self) -> str:
            return "stub"

        def validate_connection(self):  # pragma: no cover
            return (True, "ok", 0.0)

    assert _Stub()._normalize_speaker_label("SPEAKER_1") == "SPEAKER_01"


def test_local_diarization_provider_normalizes():
    """`local_provider.py:91` call site — previously raised on every segment."""
    assert LocalDiarizationProvider()._normalize_speaker_label("SPEAKER_1") == "SPEAKER_01"


def test_pyannote_cloud_diarization_provider_normalizes():
    """`pyannote_provider.py:476` call site — the cloud-diarization happy path."""
    provider = PyAnnoteCloudDiarizationProvider(api_key="test-key")
    assert provider._normalize_speaker_label("SPEAKER_2") == "SPEAKER_02"


def test_asr_and_diarization_hierarchies_agree():
    """Both hierarchies must delegate to the same implementation."""
    from app.services.asr.deepgram_provider import DeepgramProvider

    asr = DeepgramProvider(api_key="test-key")
    diar = LocalDiarizationProvider()
    for raw in ("S1", "A", "0", "speaker_0", "spk_3", "Guest-2", "SPEAKER_1", None):
        assert asr._normalize_speaker_label(raw) == diar._normalize_speaker_label(raw)


# ── Shared error sanitization ────────────────────────────────────────────────────


def test_sanitize_provider_error_scrubs_credentials():
    msg = "auth failed for Bearer abc123 using sk-secretvalue"
    out = sanitize_provider_error(msg, api_key="sk-secretvalue")
    assert "abc123" not in out
    assert "secretvalue" not in out
    assert "***" in out


def test_sanitize_provider_error_handles_empty():
    assert sanitize_provider_error("") == ""


def test_diarization_sanitize_matches_asr():
    from app.services.asr.deepgram_provider import DeepgramProvider

    msg = "boom Bearer tok dg_key123"
    assert DeepgramProvider(api_key="k")._sanitize_error(msg, "k") == (
        LocalDiarizationProvider()._sanitize_error(msg, "k")
    )
