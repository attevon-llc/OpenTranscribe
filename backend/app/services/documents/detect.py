"""Document format detection — magic bytes, ZIP disambiguation, and a text heuristic.

Deliberately **not** an extension of ``utils/file_validation.MAGIC_SIGNATURES``. That
table's ``validate_magic_bytes`` carries an audio/video browser-quirk allowance which,
applied to documents, would accept a PDF declared as ``application/msword``. A parallel
table keeps the media validator byte-identical.

Three things extensions alone cannot do:

* **DOCX, XLSX, PPTX, ODT and EPUB all start ``PK\\x03\\x04``.** Telling them apart needs
  the ZIP central directory, i.e. ~512 header bytes, not 64.
* **``.txt``/``.md``/``.csv`` have no magic bytes at all.** They get a decodability
  heuristic instead, which must be BOM-aware — the corpora contain UTF-16 fixtures whose
  first bytes look like binary.
* **Legacy OLE2 (``.doc``/``.xls``/``.ppt``) and RTF share one signature each** and are
  only parseable by the Tika tier. Detecting them precisely is what lets the upload path
  return "convert to .docx or .pdf first" instead of a generic rejection.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

#: Enough for the ZIP local header plus, in practice, the first member's name. The
#: central directory needs the tail of the file, so :func:`sniff_zip_format` takes the
#: whole buffer when it has it and falls back to the first member name when it does not.
HEADER_BYTES = 512

#: Signature → mime, longest prefix first so ``PK\x03\x04`` never shadows a longer match.
DOCUMENT_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"{\\rtf", "application/rtf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),  # legacy OLE2
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"PK\x03\x04", "application/zip"),  # refined by sniff_zip_format
)

#: WEBP is ``RIFF<4-byte size>WEBP`` — the only accepted format whose signature is not a
#: plain prefix, so it gets its own check rather than a wildcard in the table above.
_RIFF_SUBTYPES: dict[bytes, str] = {b"WEBP": "image/webp"}

#: Member path → mime, for the ZIP-container formats. Checked in order.
_ZIP_MARKERS: tuple[tuple[str, str], ...] = (
    (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ("mimetypeapplication/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.text"),
    (
        "mimetypeapplication/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    ),
    ("META-INF/container.xml", "application/epub+zip"),
)

#: Extension → mime for the formats with no magic bytes. The value is a *claim*; the text
#: heuristic still has to agree before it is believed.
_TEXTUAL_EXTENSIONS: dict[str, str] = {
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".log": "text/plain",
}

#: ``.xml`` and ``.json`` are deliberately **absent** from the table above and from
#: :data:`DOCUMENT_MIME_TYPES`. Measured reason: routing every ``.xml`` to Docling's JATS
#: backend produced 8 typed rejections and **11 bare ``AttributeError``s** over the 21 XML
#: files in ``docling-fixtures`` — USPTO patent XML, XSD schemas and ``.nxml`` all look
#: identical to a JATS article by extension. Neither is in #362's v1 format list; PMC's
#: JATS is *structural ground truth for the tests*, not an ingest format. Adding XML back
#: needs a root-element sniff, not an extension.

#: Every mime the document plane accepts, mapped to a short human name used in errors and
#: in ``document.doc_type``.
DOCUMENT_MIME_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/epub+zip": "epub",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "text/html": "html",
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/bmp": "image",
    "image/tiff": "image",
    "image/webp": "image",
    # Legacy long tail — only the Tika tier claims these.
    "application/x-ole-storage": "ole2",
    "application/rtf": "rtf",
}

#: The legacy formats no Docling tier can read. Kept as its own set so the upload path can
#: give the "convert to .docx or .pdf first" message when Tika is not configured.
LEGACY_MIME_TYPES: frozenset[str] = frozenset({"application/x-ole-storage", "application/rtf"})

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def sniff_zip_format(header: bytes, whole: bytes | None = None) -> str:
    """Refine ``application/zip`` to the OOXML/ODF/EPUB member it actually contains.

    Args:
        header: At least :data:`HEADER_BYTES` from the start of the file.
        whole: The complete bytes when available — the central directory lives at the
            *end*, so a header-only probe can only see the first member's name.

    Returns:
        A specific mime, or ``application/zip`` when nothing identifies it.
    """
    if whole is not None:
        try:
            with zipfile.ZipFile(BytesIO(whole)) as zf:
                names = set(zf.namelist())
                # ODF declares itself in an uncompressed first member named `mimetype`.
                if "mimetype" in names:
                    declared = zf.read("mimetype").decode("ascii", "ignore").strip()
                    for marker, mime in _ZIP_MARKERS:
                        if marker == f"mimetype{declared}":
                            return mime
                for marker, mime in _ZIP_MARKERS:
                    if marker.startswith("mimetype"):
                        continue
                    if marker in names:
                        return mime
        except (zipfile.BadZipFile, KeyError, OSError):
            return "application/zip"
        return "application/zip"

    # Header-only: the local header of the first member carries its name.
    for marker, mime in _ZIP_MARKERS:
        if marker.startswith("mimetype"):
            continue
        if marker.encode("ascii") in header:
            return mime
    if b"mimetypeapplication/vnd.oasis.opendocument.text" in header:
        return "application/vnd.oasis.opendocument.text"
    if b"mimetypeapplication/vnd.oasis.opendocument.spreadsheet" in header:
        return "application/vnd.oasis.opendocument.spreadsheet"
    return "application/zip"


def looks_like_text(sample: bytes) -> bool:
    """BOM-aware decodability heuristic for the formats with no magic bytes.

    A buffer is text when it decodes under a plausible encoding, contains no NUL outside a
    UTF-16/32 encoding's own padding, and is at least 90 % printable. The BOM check comes
    first because UTF-16 text is roughly half NUL bytes and every NUL-based heuristic
    calls it binary.
    """
    if not sample:
        return True
    for bom, _encoding in _BOMS:
        if sample.startswith(bom):
            # A BOM is the format declaring itself; a truncated multi-byte sequence at
            # the sample boundary is expected and says nothing about the whole file.
            return True
    if b"\x00" in sample:
        return False
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = sample.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes any byte string
            return False
    if not decoded:
        return True
    printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\r\n\t\f\v")
    return printable / len(decoded) > 0.9


def detect_document_mime(filename: str, header: bytes, whole: bytes | None = None) -> str | None:
    """Best-effort mime for a candidate document, or ``None`` if it is not one.

    Magic bytes win over the extension. That ordering is the point: an executable renamed
    ``report.pdf`` is rejected here rather than handed to a parser, and a ``.docx`` that is
    really a ``.xlsx`` is routed by content.

    Args:
        filename: Used only for the no-magic-bytes formats.
        header: The first :data:`HEADER_BYTES` bytes (more is fine).
        whole: Complete bytes when cheap to supply — improves ZIP disambiguation.

    Returns:
        A key of :data:`DOCUMENT_MIME_TYPES`, or ``None``.
    """
    if header[:4] == b"RIFF" and header[8:12] in _RIFF_SUBTYPES:
        return _RIFF_SUBTYPES[header[8:12]]

    for signature, mime in DOCUMENT_MAGIC_SIGNATURES:
        if header.startswith(signature):
            if mime == "application/zip":
                refined = sniff_zip_format(header, whole)
                return refined if refined in DOCUMENT_MIME_TYPES else None
            return mime

    suffix = filename.lower().rsplit(".", 1)
    ext = f".{suffix[1]}" if len(suffix) == 2 else ""
    claimed = _TEXTUAL_EXTENSIONS.get(ext)
    if claimed and looks_like_text(header):
        return claimed
    return None


def guess_document_mime(filename: str) -> str | None:
    """Extension-only guess — the sibling of ``watch_sources.guess_media_mime``.

    Used by the watch-source scanner, which lists names long before it has bytes. The
    content check still runs at ingest; this only decides whether a remote file is worth
    downloading at all.
    """
    lowered = filename.lower()
    ext = f".{lowered.rsplit('.', 1)[1]}" if "." in lowered else ""
    by_extension: dict[str, str] = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".odt": "application/vnd.oasis.opendocument.text",
        ".ods": "application/vnd.oasis.opendocument.spreadsheet",
        ".epub": "application/epub+zip",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
        ".doc": "application/x-ole-storage",
        ".xls": "application/x-ole-storage",
        ".ppt": "application/x-ole-storage",
        ".rtf": "application/rtf",
        **_TEXTUAL_EXTENSIONS,
    }
    return by_extension.get(ext)
