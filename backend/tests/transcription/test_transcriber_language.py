"""Tests for the transcriber's task/language capability guard.

``_resolve_task_and_language`` is the runtime safety net that enforces a model's
language capabilities regardless of what the user requested in the settings UI:

* English-only models (CrisperWhisper, ``.en`` variants) never decode a non-English
  source language — it is forced to ``"en"``.
* Translation is only requested for models that advertise ``supports_translation``.

These are pure-function tests (no model load, no GPU) so they run in any context.
"""

from __future__ import annotations

import logging

from app.transcription.transcriber import _resolve_task_and_language

CRISPER = "nyrahealth/faster_CrisperWhisper"


class TestEnglishOnlyLanguageGuard:
    """English-only models force a non-English source language to English."""

    def test_crisperwhisper_forces_non_english_to_en(self, caplog):
        with caplog.at_level(logging.WARNING):
            task, language = _resolve_task_and_language(CRISPER, "fr", translate_to_english=False)
        assert task == "transcribe"
        assert language == "en"
        assert "English-only" in caplog.text

    def test_crisperwhisper_keeps_english(self):
        task, language = _resolve_task_and_language(CRISPER, "en", translate_to_english=False)
        assert task == "transcribe"
        assert language == "en"

    def test_crisperwhisper_keeps_auto_detect(self):
        task, language = _resolve_task_and_language(CRISPER, "auto", translate_to_english=False)
        assert task == "transcribe"
        assert language is None

    def test_dotted_en_variant_forces_non_english_to_en(self):
        task, language = _resolve_task_and_language("small.en", "de", translate_to_english=False)
        assert task == "transcribe"
        assert language == "en"


class TestTranslationGuard:
    """Translation is only requested when the model supports it."""

    def test_crisperwhisper_translation_falls_back_to_transcribe(self, caplog):
        with caplog.at_level(logging.WARNING):
            task, language = _resolve_task_and_language(CRISPER, "en", translate_to_english=True)
        assert task == "transcribe"
        assert "does not support translation" in caplog.text

    def test_turbo_translation_falls_back_to_transcribe(self):
        task, _ = _resolve_task_and_language("large-v3-turbo", "fr", translate_to_english=True)
        assert task == "transcribe"

    def test_large_v3_translation_allowed(self):
        task, language = _resolve_task_and_language("large-v3", "fr", translate_to_english=True)
        assert task == "translate"
        assert language == "fr"


class TestMultilingualPassthrough:
    """Multilingual models keep the requested source language unchanged."""

    def test_large_v3_keeps_non_english_source(self):
        task, language = _resolve_task_and_language("large-v3", "ja", translate_to_english=False)
        assert task == "transcribe"
        assert language == "ja"

    def test_multilingual_auto_detect(self):
        task, language = _resolve_task_and_language("medium", "auto", translate_to_english=False)
        assert task == "transcribe"
        assert language is None
