"""Parser inputs, options, and the typed error hierarchy every backend raises.

The errors carry an ``error_category`` string because that is what ``media_file`` stores
and what the frontend maps to a suggestion. A backend that raises a bare ``Exception``
lands the document in ``processing_error`` with no guidance, which for a document is
almost always the wrong answer — "this PDF is password-protected" and "this format needs
the Tika tier" are both actionable, and neither is a bug report.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from io import BytesIO
from pathlib import Path

#: OCR policy. ``auto`` runs OCR only when the source has no usable text layer; ``force``
#: OCRs regardless (a text layer can itself be garbage — a bad prior OCR baked into the
#: PDF); ``never`` accepts an empty extraction, which is a **normal outcome** for a scan
#: with OCR off and must not be reported as an error.
OCR_POLICIES: frozenset[str] = frozenset({"auto", "force", "never"})


@dataclass(slots=True)
class ParseSource:
    """What to parse. Exactly one of ``path`` or ``data`` is set."""

    filename: str
    mime: str
    path: Path | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("ParseSource needs exactly one of path or data")

    @property
    def size(self) -> int:
        if self.data is not None:
            return len(self.data)
        assert self.path is not None
        return self.path.stat().st_size

    def read_bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        assert self.path is not None
        return self.path.read_bytes()

    def stream(self) -> BytesIO:
        return BytesIO(self.read_bytes())


@dataclass(slots=True)
class ParseOptions:
    """Per-parse knobs. Defaults come from ``SystemSettings``, not ``.env`` (repo rule)."""

    ocr: str = "auto"
    #: Hard ceiling on pages parsed. A trip appends a warning rather than failing — half a
    #: 5,000-page document is worth more than none of it, as long as the truncation is said.
    max_pages: int = 2000
    #: Below this many extracted characters per page a PDF is treated as having no usable
    #: text layer. Measured against the corpora: ``old_scans`` sits at 0 chars/page and
    #: ``olmocr-bench/tables`` at ~2,300, so the threshold is nowhere near either.
    ocr_text_threshold: int = 100
    language: str | None = None
    #: Only OCR these 1-based pages. Set by a shard task; ``None`` means the whole document.
    page_range: tuple[int, int] | None = None
    #: Pages per OCR batch inside one shard. Follows ``SEARCH_NEURAL_BATCH_SIZE``'s
    #: precedent: batch by default, retry once unbatched on failure.
    ocr_batch_size: int = 4
    extra: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ocr not in OCR_POLICIES:
            raise ValueError(f"unknown ocr policy {self.ocr!r}; expected one of {OCR_POLICIES}")


class DocumentParseError(Exception):
    """Base for every parse failure a backend is allowed to raise."""

    #: Stored on ``media_file.error_category``; drives the frontend's suggestion text.
    error_category = "processing_error"
    #: Whether re-running the same parse could plausibly succeed. ``False`` means the
    #: pipeline must not retry — a password-protected PDF is not a transient failure, and
    #: retrying it three times just delays the error the user needs to see.
    retryable = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class DocumentUnsupportedError(DocumentParseError):
    """No registered backend claims this format."""

    error_category = "format_issue"


class DocumentEncryptedError(DocumentParseError):
    """Password-protected or DRM'd. Rejected rather than prompted for (plan, `safety.py`)."""

    error_category = "format_issue"


class DocumentUnsafeError(DocumentParseError):
    """A container-safety guard tripped: zip bomb, traversal, XXE, member count.

    The object is **retained** for admin review (``media_file.is_quarantined``) rather
    than deleted — a hostile upload is evidence.
    """

    error_category = "format_issue"


class DocumentTooLargeError(DocumentParseError):
    """Past a declared ceiling (bytes, pages, or members) before any parsing happened."""

    error_category = "format_issue"


class DocumentParserUnavailableError(DocumentParseError):
    """The chosen tier could not be reached — sidecar down, Tika absent, extra missing.

    Retryable: unlike every other error here, the *document* is fine. The parse task
    leaves the row at ``pending`` for the retry sweep instead of burning it to ``failed``.
    """

    error_category = "processing_error"
    retryable = True


class DocumentEmptyError(DocumentParseError):
    """Parsed cleanly and produced no text at all, with OCR unavailable or disabled.

    Not the same as a failure: for a scan with OCR off this is the expected outcome and
    the UI shows a "re-parse with OCR" call to action. It is an exception only so the
    parse task has one place to decide between ``no_text`` and ``failed``.
    """

    error_category = "file_quality"
