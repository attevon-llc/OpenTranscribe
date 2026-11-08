"""ISO 639-1 language code -> BCP-47 locale, for vendors that reject a bare code.

Azure Speech (``speech_recognition_language``) and Google Speech-to-Text (``language_code``)
take a locale such as ``fr-FR``; the pipeline's language is an ISO code such as ``fr`` (what the
Whisper list and a user's forced reprocess language use, issue #1263). A code that is already a
locale, or one with no entry here, passes through unchanged so the vendor's own error names it.
``auto`` is the caller's to handle: no locale means "detect" differs per vendor.
"""

# One representative locale per language, the most widely served one.
DEFAULT_LOCALE: dict[str, str] = {
    "ar": "ar-SA",
    "bg": "bg-BG",
    "bn": "bn-IN",
    "ca": "ca-ES",
    "cs": "cs-CZ",
    "cy": "cy-GB",
    "da": "da-DK",
    "de": "de-DE",
    "el": "el-GR",
    "en": "en-US",
    "es": "es-ES",
    "et": "et-EE",
    "eu": "eu-ES",
    "fa": "fa-IR",
    "fi": "fi-FI",
    "fr": "fr-FR",
    "gl": "gl-ES",
    "he": "he-IL",
    "hi": "hi-IN",
    "hr": "hr-HR",
    "hu": "hu-HU",
    "id": "id-ID",
    "is": "is-IS",
    "it": "it-IT",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "lt": "lt-LT",
    "lv": "lv-LV",
    "ms": "ms-MY",
    "nl": "nl-NL",
    "no": "nb-NO",
    "pl": "pl-PL",
    "pt": "pt-BR",
    "ro": "ro-RO",
    "ru": "ru-RU",
    "sk": "sk-SK",
    "sl": "sl-SI",
    "sr": "sr-RS",
    "sv": "sv-SE",
    "sw": "sw-KE",
    "ta": "ta-IN",
    "te": "te-IN",
    "th": "th-TH",
    "tr": "tr-TR",
    "uk": "uk-UA",
    "ur": "ur-PK",
    "vi": "vi-VN",
    "zh": "zh-CN",
}


def to_locale(language: str) -> str:
    """The BCP-47 locale for *language*; already-a-locale and unknown codes pass through."""
    if "-" in language:
        return language
    return DEFAULT_LOCALE.get(language.lower(), language)
