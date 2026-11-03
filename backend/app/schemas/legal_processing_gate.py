"""Wire contract of ``/admin/legal-processing-gate`` (``app.legal.processing_gate``).

Refusal codes a processing route can answer with, in ``detail.code``:
``legal_acceptance_required`` (403), ``legal_document_unavailable`` (403) and
``legal_gate_unavailable`` (503); ``detail.documents`` lists the document keys involved.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import Field

AcceptanceScope = Literal["user", "organization", "either"]


class ProcessingGateDocument(BaseModel):
    """A document the active provider serves, offered as a requirement."""

    key: str
    title: str
    version: str


class ProcessingGateSettings(BaseModel):
    """The gate's configuration. ``required_document_keys`` empty means the gate is off."""

    required_document_keys: list[str]
    acceptance_scope: AcceptanceScope
    enabled: bool
    available_documents: list[ProcessingGateDocument]


class ProcessingGateSettingsUpdate(BaseModel):
    """Partial update. Every key must be served by the active document provider."""

    required_document_keys: list[str] | None = Field(default=None, max_length=50)
    acceptance_scope: AcceptanceScope | None = None
