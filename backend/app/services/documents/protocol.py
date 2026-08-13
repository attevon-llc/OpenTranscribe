"""The ``DocumentParser`` contract every backend satisfies.

Structural, not inherited — the same shape ``services/interfaces.py`` uses for
``StorageService`` and friends. A backend is a module that exports a class satisfying
this; nothing imports a concrete backend by name outside :mod:`registry`.
"""

from __future__ import annotations

from typing import Protocol
from typing import runtime_checkable

from .ir import ParsedDocument
from .types import ParseOptions
from .types import ParseSource


@runtime_checkable
class DocumentParser(Protocol):
    """Turn bytes into a validated :class:`~app.services.documents.ir.ParsedDocument`."""

    #: Stable identifier persisted to ``document.parser`` — ``"docling.slim"``,
    #: ``"docling.serve"``, ``"tika"``. Reparse sweeps compare on it.
    name: str

    #: Persisted to ``document.parser_version``. For the sidecar this is the *remote*
    #: version, discovered at health-check time: a document parsed by sidecar 1.30 and one
    #: parsed by 1.34 are not interchangeable, and recording the local client's version
    #: instead would make them look identical.
    version: str

    def supports(self, mime: str, filename: str, *, needs_ocr: bool) -> bool:
        """Can this backend parse it? ``needs_ocr`` excludes tiers with no OCR."""
        ...

    def parse(self, source: ParseSource, *, options: ParseOptions) -> ParsedDocument:
        """Parse, or raise a :class:`~app.services.documents.types.DocumentParseError`.

        Implementations must return a document that has been through
        :func:`~app.services.documents.ir.validate_ir` — in practice by building it with
        :class:`~app.services.documents.ir.IRBuilder`, whose ``build`` validates.
        """
        ...

    def health(self) -> tuple[bool, str]:
        """``(available, detail)``. Called by the registry's ``auto`` resolution and by
        the admin health card, so it must be cheap and must never raise."""
        ...
