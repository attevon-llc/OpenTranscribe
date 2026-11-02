"""Reading and writing the legal processing gate's two settings (admin side)."""

from __future__ import annotations

from typing import Any
from typing import cast

from sqlalchemy.orm import Session

from app.core.constants import LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY
from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.legal import processing_gate
from app.legal import registry
from app.schemas.legal_processing_gate import AcceptanceScope
from app.schemas.legal_processing_gate import ProcessingGateDocument
from app.schemas.legal_processing_gate import ProcessingGateSettings

_DESCRIPTIONS = {
    LEGAL_PROCESSING_REQUIRED_KEYS_KEY: (
        "Comma-separated legal document keys a file's owner must have accepted before the "
        "file is processed. Empty = off."
    ),
    LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY: (
        "Whose acceptance satisfies the processing requirement: user, organization or either."
    ),
}


class UnknownDocumentError(ValueError):
    """A requested key is not served by the active document provider."""

    def __init__(self, keys: list[str]):
        super().__init__(", ".join(keys))
        self.keys = keys


def unserved_keys(keys: list[str]) -> list[str]:
    """The keys in *keys* the active document provider does not serve (deduplicated)."""
    wanted = processing_gate.parse_required_keys(",".join(keys))
    return [k for k in wanted if registry.get_document(k) is None]


def read_settings(db: Session) -> ProcessingGateSettings:
    """The stored configuration (fresh read) plus the documents that can be required."""
    processing_gate.invalidate_cache()
    config = processing_gate.read_config(db)
    return ProcessingGateSettings(
        required_document_keys=list(config.required_keys),
        acceptance_scope=cast(AcceptanceScope, config.scope),
        enabled=bool(config.required_keys),
        available_documents=[
            ProcessingGateDocument(key=d.key, title=d.title, version=d.version)
            for d in registry.list_documents()
        ],
    )


def update_settings(
    db: Session, *, required_keys: list[str] | None, scope: str | None
) -> list[dict[str, Any]]:
    """Write what was given; returns one ``{setting, from, to}`` per change.

    Raises:
        UnknownDocumentError: A key the active provider does not serve. Nothing is written.
    """
    from app.services.system_settings_service import set_setting

    before = read_settings(db)
    changes: list[dict[str, Any]] = []
    if required_keys is not None:
        keys = list(processing_gate.parse_required_keys(",".join(required_keys)))
        unknown = unserved_keys(keys)
        if unknown:
            raise UnknownDocumentError(unknown)
        if keys != before.required_document_keys:
            set_setting(
                db,
                LEGAL_PROCESSING_REQUIRED_KEYS_KEY,
                ",".join(keys),
                _DESCRIPTIONS[LEGAL_PROCESSING_REQUIRED_KEYS_KEY],
            )
            changes.append(
                {
                    "setting": LEGAL_PROCESSING_REQUIRED_KEYS_KEY,
                    "from": before.required_document_keys,
                    "to": keys,
                }
            )
    if scope is not None and scope != before.acceptance_scope:
        set_setting(
            db,
            LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY,
            scope,
            _DESCRIPTIONS[LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY],
        )
        changes.append(
            {
                "setting": LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY,
                "from": before.acceptance_scope,
                "to": scope,
            }
        )
    processing_gate.invalidate_cache()
    return changes
