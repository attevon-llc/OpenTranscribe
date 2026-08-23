"""Pre-parse containment for hostile documents.

**The parser only ever runs in a worker or the sidecar; the API reads at most
:data:`~app.services.documents.detect.HEADER_BYTES`.** That boundary is load-bearing and
every check here assumes it — these guards bound the damage a hostile file can do to a
worker, they are not a substitute for the worker being the only place it is opened.

Four attack shapes, all of which are cheap to defend and expensive to discover in
production:

* **Zip bombs.** Every Office format is a ZIP. ``zipfile`` will happily report a 10 KB
  archive that expands to 2 GB, and the expansion happens inside the parser where no
  ceiling applies. Checked from the central directory *before* any member is read.
* **Path traversal.** An absolute or ``..``-relative member name matters the moment any
  backend extracts to disk. Rejected regardless of whether the current backend does.
* **XXE and billion-laughs.** Every OOXML part is XML. ``defusedxml.defuse_stdlib()`` is
  called at package import so a backend reaching for ``xml.etree`` gets the hardened one.
* **Runaway page counts.** Capped before parsing, not after.
"""

from __future__ import annotations

import logging
import zipfile
from io import BytesIO

from .types import DocumentEncryptedError
from .types import DocumentTooLargeError
from .types import DocumentUnsafeError

logger = logging.getLogger(__name__)

#: Total uncompressed size across all members. 512 MB is far above any real document
#: (the largest in the acquired corpora is a 34 MB scan) and far below what would exhaust
#: a worker.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

#: Compression ratio ceiling. Real OOXML sits around 3-10:1; a zip bomb is 1000:1+.
MAX_COMPRESSION_RATIO = 200.0

#: Member count ceiling. A DOCX with 400 embedded images is plausible; 5,000 is not.
MAX_ZIP_MEMBERS = 5000

#: Nesting ceiling — an archive inside an archive inside an archive is a bomb pattern, and
#: no document format we accept needs more than one level (EPUB, ODF and OOXML are flat).
MAX_ZIP_DEPTH = 1

_ARCHIVE_SUFFIXES = (".zip", ".jar", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz")


def defuse_xml() -> None:
    """Harden the stdlib XML parsers process-wide. Idempotent; called at package import.

    ``defusedxml`` is already a dependency (``requirements.txt``) and is already used by
    the OOXML libraries' transitive stack, but nothing had ever called ``defuse_stdlib``.
    Without it a crafted ``document.xml`` reaches ``xml.etree`` with entity expansion on.
    """
    try:
        import defusedxml

        defusedxml.defuse_stdlib()
    except Exception as exc:  # noqa: BLE001 - hardening must never break importability
        logger.warning("defusedxml.defuse_stdlib() failed: %s", exc)


def assert_zip_is_safe(data: bytes, *, depth: int = 0) -> None:
    """Raise :class:`DocumentUnsafeError`/:class:`DocumentTooLargeError` for a hostile ZIP.

    Reads only the central directory, so the cost is independent of the declared expanded
    size — which is the point: discovering a bomb by decompressing it is not a defence.

    Args:
        data: The complete archive bytes.
        depth: Current nesting level; incremented for members that are themselves archives.
    """
    if depth > MAX_ZIP_DEPTH:
        raise DocumentUnsafeError(f"archive nested deeper than {MAX_ZIP_DEPTH} level(s)")

    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = zf.infolist()

            if len(infos) > MAX_ZIP_MEMBERS:
                raise DocumentTooLargeError(
                    f"archive has {len(infos)} members, over the {MAX_ZIP_MEMBERS} ceiling"
                )

            total_uncompressed = 0
            for info in infos:
                name = info.filename
                if name.startswith(("/", "\\")):
                    raise DocumentUnsafeError(f"archive member has an absolute path: {name!r}")
                if ".." in name.replace("\\", "/").split("/"):
                    raise DocumentUnsafeError(f"archive member escapes its root: {name!r}")
                if info.flag_bits & 0x1:
                    raise DocumentEncryptedError(f"archive member {name!r} is encrypted")

                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise DocumentTooLargeError(
                        f"archive expands to over {MAX_UNCOMPRESSED_BYTES} bytes"
                    )

                # Per-member ratio, so one 1000:1 member is caught even when the archive's
                # aggregate ratio is diluted by honest members alongside it.
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > MAX_COMPRESSION_RATIO:
                        raise DocumentUnsafeError(
                            f"archive member {name!r} has a {ratio:.0f}:1 compression ratio, "
                            f"over the {MAX_COMPRESSION_RATIO:.0f}:1 ceiling"
                        )

                # Only now is a member actually read, and only because it claims to be
                # an archive. Its own size was already counted above.
                if (
                    depth < MAX_ZIP_DEPTH
                    and name.lower().endswith(_ARCHIVE_SUFFIXES)
                    and info.file_size <= MAX_UNCOMPRESSED_BYTES
                ):
                    assert_zip_is_safe(zf.read(name), depth=depth + 1)

    except zipfile.BadZipFile as exc:
        raise DocumentUnsafeError("not a readable archive", detail=str(exc)) from exc


def assert_pdf_is_readable(data: bytes) -> None:
    """Reject encrypted PDFs before a backend prompts, hangs, or silently returns nothing.

    ``pypdfium2`` raises ``PdfiumError`` naming an incorrect password; the docling and
    Tika tiers each fail differently, so the check is done once here rather than three
    times inconsistently. Verified against
    ``docling-fixtures/tests/data/pdf_password/sources/2206.01062_pg3.pdf``.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - the slim tier always has it
        return

    try:
        pdf = pdfium.PdfDocument(BytesIO(data))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise DocumentEncryptedError("this PDF is password-protected", detail=str(exc)) from exc
        raise DocumentUnsafeError("PDF could not be opened", detail=str(exc)) from exc
    else:
        pdf.close()


def assert_page_count(pages: int, max_pages: int) -> None:
    """Raise when a document declares more pages than the ceiling allows."""
    if pages > max_pages:
        raise DocumentTooLargeError(f"{pages} pages, over the {max_pages}-page ceiling")


def prescan(mime: str, data: bytes, *, max_pages: int = 2000) -> None:
    """Run every guard that applies to *mime*. The one call a backend makes.

    Backends call this rather than picking guards themselves, so adding a check protects
    every tier at once instead of the one whose author remembered.
    """
    if mime == "application/pdf":
        assert_pdf_is_readable(data)
        return
    if data[:4] == b"PK\x03\x04":
        assert_zip_is_safe(data)
