"""The ONE parser for a language value, wherever it came from (issue #545).

``MediaFile.language`` is written by whichever ASR provider ran, and they do not agree on
shape: ``en``, ``EN``, ``en-US``, ``en_GB``, ``eng``, ``English`` and ``"en "`` all occur, as
do the sentinels a detector writes when it declined to commit (``auto``, ``und``, ``unknown``,
``""``). Every reader of that column has to reduce it to something comparable, and until this
module there were **two** implementations doing so — ``services/chat/language.py`` and
``services/redaction/config.py`` — which **disagreed on 13 of 21 inputs**:

* ``None`` / ``""`` / ``auto`` / ``und`` / ``unknown`` → ``None`` (chat) vs ``"en"`` (redaction)
* ``none`` / ``null`` / ``nan`` / ``undefined`` / ``"  "`` / ``123`` / ``e1`` → ``None`` (chat)
  vs **passed through unchanged** (redaction)

Each call site imported the one its own domain wanted, which is why that was a latent hazard
rather than a live defect — but the redaction half was live on its own account, because a value
it passed through unchanged is absent from ``REDACTION_PII_LANGUAGES`` and so silently
*disabled* the PII detector while ``coverage.py`` subtracted the skip and reported no gap.

**The contract, and the one decision in it:**

``normalize_language`` returns a bare code it recognises, or ``default`` (``None`` unless the
caller asks otherwise) for anything it cannot identify. ⚠️ **It must never fall back to
``"en"``.** That was an earlier draft of the redaction fix and it is worse than the bug: it maps
``"fra"`` onto ``"en"``, runs the **English** PII detector over French text, and records full
coverage — a new fail-open, against a contract that says PII support is English-only by design.
``None`` means "we could not determine this recording's language", which every caller can then
handle honestly: chat counts it as ``unknown_files`` and warns about nothing, redaction fails
CLOSED and reports a real coverage gap.

The recognised vocabulary is the app's own — ``constants.WHISPER_LANGUAGES``, which is
cross-checked against ``faster_whisper``'s code list at import — plus the ISO 639-2 three-letter
forms (both the terminological ``/T`` and bibliographic ``/B`` spellings, which differ for about
twenty languages) and the English names in that same table. A code outside it normalizes to
``None`` rather than being echoed: a two-letter string that names no language must not read
downstream as "a recognised language that happens to have no detector", because that is exactly
the shape of a *legitimate* skip and is subtracted from every coverage report.
"""

from __future__ import annotations

from app.core import constants as C  # noqa: N812

#: ISO 639-2 (and the handful of superseded ISO 639-1 spellings) onto the codes this app
#: stores. Both the ``/T`` and ``/B`` forms are listed where they differ, because providers
#: emit either. Every value is asserted to be a code the app knows by
#: ``tests/unit/test_language_normalization.py``, so a typo here cannot silently drop an alias.
_ALIAS_CODES: dict[str, str] = {
    "eng": "en",
    "zho": "zh",
    "chi": "zh",
    "deu": "de",
    "ger": "de",
    "spa": "es",
    "rus": "ru",
    "kor": "ko",
    "fra": "fr",
    "fre": "fr",
    "jpn": "ja",
    "por": "pt",
    "tur": "tr",
    "pol": "pl",
    "cat": "ca",
    "nld": "nl",
    "dut": "nl",
    "ara": "ar",
    "swe": "sv",
    "ita": "it",
    "ind": "id",
    "hin": "hi",
    "fin": "fi",
    "vie": "vi",
    "heb": "he",
    "ukr": "uk",
    "ell": "el",
    "gre": "el",
    "msa": "ms",
    "may": "ms",
    "ces": "cs",
    "cze": "cs",
    "ron": "ro",
    "rum": "ro",
    "dan": "da",
    "hun": "hu",
    "tam": "ta",
    "nor": "no",
    "tha": "th",
    "urd": "ur",
    "hrv": "hr",
    "bul": "bg",
    "lit": "lt",
    "lat": "la",
    "mri": "mi",
    "mao": "mi",
    "mal": "ml",
    "cym": "cy",
    "wel": "cy",
    "slk": "sk",
    "slo": "sk",
    "tel": "te",
    "fas": "fa",
    "per": "fa",
    "lav": "lv",
    "ben": "bn",
    "srp": "sr",
    "aze": "az",
    "slv": "sl",
    "kan": "kn",
    "est": "et",
    "mkd": "mk",
    "mac": "mk",
    "bre": "br",
    "eus": "eu",
    "baq": "eu",
    "isl": "is",
    "ice": "is",
    "hye": "hy",
    "arm": "hy",
    "nep": "ne",
    "mon": "mn",
    "bos": "bs",
    "kaz": "kk",
    "sqi": "sq",
    "alb": "sq",
    "swa": "sw",
    "glg": "gl",
    "mar": "mr",
    "pan": "pa",
    "sin": "si",
    "khm": "km",
    "sna": "sn",
    "yor": "yo",
    "som": "so",
    "afr": "af",
    "oci": "oc",
    "kat": "ka",
    "geo": "ka",
    "bel": "be",
    "tgk": "tg",
    "snd": "sd",
    "guj": "gu",
    "amh": "am",
    "yid": "yi",
    "lao": "lo",
    "uzb": "uz",
    "fao": "fo",
    "hat": "ht",
    "pus": "ps",
    "tuk": "tk",
    "nno": "nn",
    "mlt": "mt",
    "san": "sa",
    "ltz": "lb",
    "mya": "my",
    "bur": "my",
    "bod": "bo",
    "tib": "bo",
    "tgl": "tl",
    "fil": "tl",
    "mlg": "mg",
    "asm": "as",
    "tat": "tt",
    "lin": "ln",
    "hau": "ha",
    "bak": "ba",
    "jav": "jw",
    "jv": "jw",
    "sun": "su",
    "iw": "he",
    "in": "id",
    "ji": "yi",
    "mo": "ro",
}


def _build_vocabulary() -> dict[str, str]:
    """Every spelling this parser recognises → the code it normalizes to.

    Built from the app's own language table rather than a second hand-maintained list, so a
    language added for transcription is understood here on the same commit. ``auto`` is
    excluded deliberately: it is the "not detected yet" sentinel, not a language.
    """
    codes = {code: code for code in C.WHISPER_LANGUAGES if code != "auto"}
    names = {name.lower(): code for code, name in C.WHISPER_LANGUAGES.items() if code in codes}
    return {**codes, **names, **_ALIAS_CODES}


_VOCABULARY: dict[str, str] = _build_vocabulary()


def normalize_language(raw: str | None, *, default: str | None = None) -> str | None:
    """Reduce a stored or provider-supplied language value to a bare code.

    Case, surrounding whitespace and the region subtag are all irrelevant to every caller —
    English is English whether it arrives as ``en``, ``EN``, ``en-US`` or ``en_GB`` — so they
    are stripped before the lookup.

    Args:
        raw: The value as stored or as the provider returned it. May be ``None``, blank, a
            sentinel (``auto`` / ``und`` / ``unknown``), a code, or a language name.
        default: What to return when the value names no language this app knows. Keyword-only
            and ``None`` by default, so a caller that wants a fallback has to say so at the
            call site. ⚠️ **Do not pass ``"en"`` to make an unrecognised language "work"** —
            see the module docstring.

    Returns:
        A lowercase code from :data:`~app.core.constants.WHISPER_LANGUAGES`, else ``default``.
    """
    if raw is None:
        return default
    text = " ".join(str(raw).split()).lower()
    if not text:
        return default
    # Full string first: a language NAME can contain a space or a hyphen ("haitian creole"),
    # so splitting before the lookup would truncate it.
    code = _VOCABULARY.get(text)
    if code is None:
        code = _VOCABULARY.get(text.replace("_", "-").split("-", 1)[0])
    return code if code is not None else default
