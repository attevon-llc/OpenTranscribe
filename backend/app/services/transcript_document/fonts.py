"""The fonts a PDF embeds: a pinned Noto set, chosen from the text that is actually printed.

The images carry the fonts under ``DOCUMENT_FONT_DIR`` (the ``fonts`` stage of the three backend
Dockerfiles copies a fixed list of files out of Debian's ``fonts-noto-core`` and
``fonts-noto-cjk`` and writes the SIL OFL 1.1 text beside them). Every font is SIL OFL 1.1; the
PDF embeds only the glyph subset a document uses, which OFL permits. The fonts are never modified.

Only the faces a document needs are loaded: Latin, Greek and Cyrillic come from ``Noto Sans``,
and each other script's face is registered as a fallback only when a character of that script
appears. That matters for the CJK collection, which is 19 MB and the slowest face to parse.

``Noto Sans`` is the one REQUIRED face. Without it there is no honest PDF to make, so the export
fails with :class:`FontsUnavailableError` rather than printing boxes. A missing script face is
not fatal: its glyphs print as ``.notdef`` and are counted (never named: they are transcript
content) so an operator can see the image lacks a font.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from fontTools.ttLib import TTCollection
from fpdf.fonts import TTFFont

from app.core.constants import DOCUMENT_FONT_DIR
from app.services.captions.scripts import CJK
from app.services.captions.scripts import HANGUL
from app.services.transcript_document.model import FontsUnavailableError
from app.services.transcript_document.scripts import cjk_region

if TYPE_CHECKING:
    from fpdf import FPDF

logger = logging.getLogger(__name__)

BASE_FAMILY = "NotoSans"
CJK_FAMILY = "NotoCJK"
_CJK_FILE = "NotoSansCJK-Regular.ttc"


@dataclass(frozen=True)
class ScriptFont:
    """One non-Latin script: the fpdf family name, the file stem and the characters it covers."""

    family: str
    stem: str
    ranges: str


#: Fallback faces, in the order fpdf2 tries them. A character takes the first face that has it.
SCRIPT_FONTS: tuple[ScriptFont, ...] = (
    ScriptFont(
        "NotoArabic",
        "NotoNaskhArabic",
        "\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff",
    ),
    ScriptFont("NotoHebrew", "NotoSansHebrew", "\u0590-\u05ff\ufb1d-\ufb4f"),
    ScriptFont("NotoDevanagari", "NotoSansDevanagari", "\u0900-\u097f\ua8e0-\ua8ff"),
    ScriptFont("NotoBengali", "NotoSansBengali", "\u0980-\u09ff"),
    ScriptFont("NotoGurmukhi", "NotoSansGurmukhi", "\u0a00-\u0a7f"),
    ScriptFont("NotoGujarati", "NotoSansGujarati", "\u0a80-\u0aff"),
    ScriptFont("NotoTamil", "NotoSansTamil", "\u0b80-\u0bff"),
    ScriptFont("NotoTelugu", "NotoSansTelugu", "\u0c00-\u0c7f"),
    ScriptFont("NotoKannada", "NotoSansKannada", "\u0c80-\u0cff"),
    ScriptFont("NotoMalayalam", "NotoSansMalayalam", "\u0d00-\u0d7f"),
    ScriptFont("NotoSinhala", "NotoSansSinhala", "\u0d80-\u0dff"),
    ScriptFont("NotoThai", "NotoSansThai", "\u0e00-\u0e7f"),
    ScriptFont("NotoLao", "NotoSansLao", "\u0e80-\u0eff"),
    ScriptFont("NotoMyanmar", "NotoSansMyanmar", "\u1000-\u109f"),
    ScriptFont("NotoGeorgian", "NotoSansGeorgian", "\u10a0-\u10ff"),
    ScriptFont("NotoEthiopic", "NotoSansEthiopic", "\u1200-\u139f"),
    ScriptFont("NotoKhmer", "NotoSansKhmer", "\u1780-\u17ff"),
    ScriptFont("NotoArmenian", "NotoSansArmenian", "\u0530-\u058f"),
)
#: Han, kana, Hangul and the CJK punctuation / full-width blocks.
_CJK_CHARS = re.compile(f"[{CJK}{HANGUL}\u2e80-\u2fdf\u3000-\u303f\uff00-\uffef]")
_STYLES = ("", "B", "I")
_STYLE_SUFFIX = {"": "Regular", "B": "Bold", "I": "Italic"}


@dataclass(frozen=True)
class Face:
    """One registered face: ``family`` + ``style`` at ``path`` (``collection`` for a ``.ttc``)."""

    family: str
    style: str
    path: Path
    collection: int = 0


@dataclass(frozen=True)
class FontPlan:
    """The faces one document needs, in registration order."""

    faces: tuple[Face, ...]
    fallbacks: tuple[str, ...]

    def install(self, pdf: FPDF) -> None:
        """Register every face with ``pdf`` and set the fallback order."""
        for face in self.faces:
            pdf.add_font(
                face.family, face.style, str(face.path), collection_font_number=face.collection
            )
        pdf.set_fallback_fonts(list(self.fallbacks))


@lru_cache(maxsize=8)
def _cjk_collection_index(path: str, region: str) -> int:
    """Index of the ``Noto Sans CJK <region>`` face inside the collection."""
    collection = TTCollection(path, lazy=True)
    wanted = f"Noto Sans CJK {region}"
    for index, font in enumerate(collection.fonts):
        if font["name"].getDebugName(1) == wanted:
            return index
    raise FontsUnavailableError(f"{wanted} is not in {_CJK_FILE}")


def _family_faces(family: str, stem: str, font_dir: Path) -> list[Face]:
    """Regular/Bold/Italic faces of one family; a missing weight borrows the regular file."""
    regular = font_dir / f"{stem}-{_STYLE_SUFFIX['']}.ttf"
    if not regular.is_file():
        return []
    faces = []
    for style in _STYLES:
        candidate = font_dir / f"{stem}-{_STYLE_SUFFIX[style]}.ttf"
        faces.append(Face(family, style, candidate if candidate.is_file() else regular))
    return faces


def build_font_plan(text: str, language: str | None, font_dir: Path | None = None) -> FontPlan:
    """Choose the faces for ``text``.

    Raises:
        FontsUnavailableError: when ``Noto Sans`` itself is not installed.
    """
    directory = font_dir or Path(DOCUMENT_FONT_DIR)
    base = _family_faces(BASE_FAMILY, "NotoSans", directory)
    if not base:
        raise FontsUnavailableError(f"NotoSans-Regular.ttf not found in {directory}")
    faces = list(base)
    fallbacks: list[str] = []
    present = set(text)
    for script in SCRIPT_FONTS:
        pattern = re.compile(f"[{script.ranges}]")
        if any(pattern.match(ch) for ch in present):
            script_faces = _family_faces(script.family, script.stem, directory)
            if script_faces:
                faces.extend(script_faces)
                fallbacks.append(script.family)
            else:
                logger.warning("Document font %s is not installed", script.stem)
    if any(_CJK_CHARS.match(ch) for ch in present):
        collection = directory / _CJK_FILE
        if collection.is_file():
            index = _cjk_collection_index(str(collection), cjk_region(language))
            # No CJK bold or italic: every weight borrows the regular face.
            faces.extend(Face(CJK_FAMILY, style, collection, index) for style in _STYLES)
            fallbacks.append(CJK_FAMILY)
        else:
            logger.warning("Document font %s is not installed", _CJK_FILE)
    return FontPlan(tuple(faces), tuple(fallbacks))


def count_missing_glyphs(pdf: FPDF, text: str) -> int:
    """How many distinct printable characters of ``text`` no registered face can draw.

    Only a COUNT is ever reported: the characters are transcript content.
    """
    covered: set[int] = set()
    for font in pdf.fonts.values():
        if isinstance(font, TTFFont):  # the built-in core fonts carry no cmap
            covered.update(font.cmap)
    return sum(
        1 for ch in set(text) if not ch.isspace() and ord(ch) >= 32 and ord(ch) not in covered
    )
