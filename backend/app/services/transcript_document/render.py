"""One entry point from a :class:`PreparedDocument` to the bytes of a DOCX or PDF file.

Kept apart from ``build`` (which needs a database session) so rendering runs with none held.
"""

from __future__ import annotations

from app.services.transcript_document.build import PreparedDocument
from app.services.transcript_document.docx_renderer import render_docx
from app.services.transcript_document.pdf_renderer import render_pdf

CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def render_document(fmt: str, prepared: PreparedDocument) -> bytes:
    """Render ``prepared`` as ``fmt`` (``docx`` or ``pdf``)."""
    if fmt == "docx":
        return render_docx(
            prepared.blocks, prepared.spec, prepared.options, prepared.labels, prepared.meta
        )
    if fmt == "pdf":
        return render_pdf(
            prepared.blocks, prepared.spec, prepared.options, prepared.labels, prepared.meta
        )
    raise ValueError(f"Unsupported document format: {fmt}")
