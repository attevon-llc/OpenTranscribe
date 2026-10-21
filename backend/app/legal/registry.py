"""Legal document registry and the extension seam for replacing the document set.

The default provider serves the documents vendored under ``app/legal/documents``. They are
copied unmodified from the central legal repository and pinned by ``MANIFEST.json``
(see ``scripts/verify_vendored.py``). This module never edits their text.

Seam (``CLOUD_SEAM_VERSION`` 9, ``app.auth.constants``): an extension calls
``set_document_provider(provider)`` once at startup to serve a different set. Installing
any provider makes ``requires_acknowledgment`` false for the operator acknowledgment,
because that document is a self-hosted-operator concern and the extension runs its own
acceptance flow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Document the administrator of a self-hosted deployment must acknowledge.
OPERATOR_ACKNOWLEDGMENT_KEY = "opentranscribe-operator-acknowledgment"

_DOCUMENTS_DIR = Path(__file__).resolve().parent / "documents"


@dataclass(frozen=True)
class LegalDocument:
    """One served document. ``content`` is the exact bytes that are hashed and shown."""

    key: str
    title: str
    version: str
    status: str
    audience: str
    content: bytes

    @property
    def sha256(self) -> str:
        """Hex SHA-256 of the exact bytes served."""
        return hashlib.sha256(self.content).hexdigest()

    @property
    def markdown(self) -> str:
        """The document text to display, without its front matter block.

        The hash and the acceptance record cover the full file (``content``); only the
        rendered text drops the metadata block, which is not part of what a reader accepts.
        """
        return strip_front_matter(self.content.decode("utf-8"))


class DocumentProvider(Protocol):
    """What an extension implements to replace the document set."""

    def list_documents(self) -> Sequence[LegalDocument]:
        """Return every document this provider serves."""
        ...

    def get_document(self, key: str) -> LegalDocument | None:
        """Return one document by key, or ``None`` when it is not served."""
        ...


def parse_front_matter(text: str) -> dict[str, str]:
    """Parse the simple ``key: value`` front matter block at the top of a document."""
    lines = text.splitlines()
    out: dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def strip_front_matter(text: str) -> str:
    """Return ``text`` without a leading ``---`` delimited front matter block."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :]).lstrip("\n")
    return text


def load_vendored_documents(directory: Path = _DOCUMENTS_DIR) -> list[LegalDocument]:
    """Load the files named by ``MANIFEST.json`` in *directory*, in manifest order."""
    manifest = json.loads((directory / "MANIFEST.json").read_text(encoding="utf-8"))
    docs: list[LegalDocument] = []
    for name in manifest["files"]:
        content = (directory / name).read_bytes()
        fm = parse_front_matter(content.decode("utf-8"))
        docs.append(
            LegalDocument(
                key=fm["key"],
                title=fm.get("title", fm["key"]),
                version=fm["version"],
                status=fm.get("status", "draft"),
                audience=fm.get("audience", ""),
                content=content,
            )
        )
    return docs


class VendoredProvider:
    """Default provider: the documents vendored in this repository."""

    def __init__(self, directory: Path = _DOCUMENTS_DIR) -> None:
        self._docs = load_vendored_documents(directory)
        self._by_key = {d.key: d for d in self._docs}

    def list_documents(self) -> Sequence[LegalDocument]:
        return list(self._docs)

    def get_document(self, key: str) -> LegalDocument | None:
        return self._by_key.get(key)


_default_provider = VendoredProvider()
_provider: DocumentProvider = _default_provider


def set_document_provider(provider: DocumentProvider | None) -> None:
    """Install *provider*, or restore the vendored default when ``None``."""
    global _provider
    _provider = _default_provider if provider is None else provider


def is_default_provider() -> bool:
    """True while the vendored OSS document set is the one being served."""
    return _provider is _default_provider


def list_documents() -> list[LegalDocument]:
    """Every document the active provider serves."""
    return list(_provider.list_documents())


def get_document(key: str) -> LegalDocument | None:
    """One document from the active provider, or ``None``."""
    return _provider.get_document(key)


def requires_acknowledgment(key: str) -> bool:
    """Whether an administrator must acknowledge *key* in this edition.

    Only the operator acknowledgment, and only while the vendored default provider is
    installed (community/self-hosted build).
    """
    return key == OPERATOR_ACKNOWLEDGMENT_KEY and is_default_provider()
