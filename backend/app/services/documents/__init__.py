"""Document parsing — the tiered Docling stack behind one registry (#362 / #403 Stage 6a).

Public surface. Import from here, not from the backend modules: the tier a document takes
is a runtime decision made in :mod:`registry`, and a call site that names a backend has
already broken it.
"""

from .detect import DOCUMENT_MIME_TYPES
from .detect import LEGACY_MIME_TYPES
from .detect import detect_document_mime
from .detect import guess_document_mime
from .ir import BLOCK_TYPES
from .ir import IR_VERSION
from .ir import Block
from .ir import IRBuilder
from .ir import IRValidationError
from .ir import ParsedDocument
from .ir import validate_ir
from .protocol import DocumentParser
from .registry import get_parser_for
from .registry import health_report
from .safety import defuse_xml
from .types import DocumentEmptyError
from .types import DocumentEncryptedError
from .types import DocumentParseError
from .types import DocumentParserUnavailableError
from .types import DocumentTooLargeError
from .types import DocumentUnsafeError
from .types import DocumentUnsupportedError
from .types import ParseOptions
from .types import ParseSource

# Every OOXML part is XML, and the parsers reach for the stdlib readers. Hardening at
# package import is the only placement that covers a backend added later without its
# author having to remember: an XXE in a crafted `document.xml` does not announce itself.
defuse_xml()

__all__ = [
    "BLOCK_TYPES",
    "DOCUMENT_MIME_TYPES",
    "IR_VERSION",
    "LEGACY_MIME_TYPES",
    "Block",
    "DocumentEmptyError",
    "DocumentEncryptedError",
    "DocumentParseError",
    "DocumentParser",
    "DocumentParserUnavailableError",
    "DocumentTooLargeError",
    "DocumentUnsafeError",
    "DocumentUnsupportedError",
    "IRBuilder",
    "IRValidationError",
    "ParseOptions",
    "ParseSource",
    "ParsedDocument",
    "defuse_xml",
    "detect_document_mime",
    "get_parser_for",
    "guess_document_mime",
    "health_report",
    "validate_ir",
]
