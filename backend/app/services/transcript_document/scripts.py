"""Which script a string is written in, and what that means for a document renderer.

Direction is a property of the CONTENT, not of the UI locale: an Arabic turn in an English
session is right-to-left, and an English turn in an Arabic session is not. The rule is the
Unicode Bidirectional Algorithm's paragraph level (UAX #9 P2/P3): the first strong character
decides, mirroring the SPA's ``dir="auto"``.

Range constants for CJK, Hangul and the unspaced scripts come from ``captions/scripts.py``
rather than a second table, so the caption segmenter and the document renderers agree on what
"unspaced" means.
"""

from __future__ import annotations

import re
import unicodedata

from app.services.captions.scripts import CJK
from app.services.captions.scripts import HANGUL
from app.services.captions.scripts import UNSPACED
from app.services.transcript_document.model import Direction

# Arabic (+ supplement, extended-A, presentation forms), Hebrew, Syriac, Thaana, N'Ko,
# the Indic blocks Devanagari..Malayalam, Sinhala, Thai, Lao, Tibetan, Myanmar, Khmer: the
# scripts whose letters join or reorder, so a line containing one needs the shaping engine.
_SHAPING_RANGES = (
    "\u0590-\u08ff"  # Hebrew, Arabic, Syriac, Arabic supplement, Thaana, N'Ko, Arabic ext-A
    "\u0900-\u0dff"  # Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Sinhala
    "\u0e00-\u0eff"  # Thai, Lao
    "\u0f00-\u0fff"  # Tibetan
    "\u1000-\u109f"  # Myanmar
    "\u1780-\u17ff"  # Khmer
    "\ufb1d-\ufdff\ufe70-\ufeff"  # Hebrew and Arabic presentation forms
)
_NEEDS_SHAPING = re.compile(f"[{_SHAPING_RANGES}]")
_UNSPACED = re.compile(f"[{UNSPACED}]")
_CJK_OR_HANGUL = re.compile(f"[{CJK}{HANGUL}]")

#: Characters that cannot be written into an XML 1.0 document (DOCX): C0 controls other than
#: tab / LF / CR, the surrogate range and the two non-characters U+FFFE / U+FFFF.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def base_direction(text: str, default: Direction = "ltr") -> Direction:
    """``rtl`` when the first strong character is right-to-left, ``ltr`` when it is left-to-right.

    A string with no strong character (digits, punctuation, empty) returns ``default``.
    """
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ("R", "AL"):
            return "rtl"
        if bidi == "L":
            return "ltr"
    return default


def needs_shaping(text: str) -> bool:
    """True when ``text`` contains a script whose letters join or reorder (Arabic, Indic, ...).

    Shaping runs per line and only for such a line: measured on a four-hour Latin transcript,
    shaping every line cost four times the render with no visible difference.
    """
    return _NEEDS_SHAPING.search(text) is not None


def has_unspaced_script(text: str) -> bool:
    """True when ``text`` contains a script with no spaces between words (Thai, Lao, ...)."""
    return _UNSPACED.search(text) is not None


def breaks_between_characters(text: str) -> bool:
    """True when a line may be cut between any two characters (CJK, Hangul and unspaced scripts).

    These lines wrap by character: a word-based wrap would see one enormous "word". Kinsoku
    (the rule that a line may not start with a closing mark) is NOT applied by the document
    renderers; the caption segmenter has it, a transcript page does not.
    """
    return has_unspaced_script(text) or _CJK_OR_HANGUL.search(text) is not None


def sanitize_for_xml(text: str) -> str:
    """Drop the characters XML 1.0 forbids; python-docx raises ``ValueError`` on any of them."""
    return _XML_ILLEGAL.sub("", text)
