"""Required legal documents before processing (the "processing gate").

An operator can require that the OWNER of a file has accepted the current text of one or more
legal documents before anything processes that file: transcription, speaker detection, URL
ingest, and the AI steps that run on an existing transcript (summaries, redaction detection,
speaker identification). Two ``SystemSettings`` rows drive it
(``core/constants.LEGAL_PROCESSING_*``); an empty key list, the community default, turns it off.

Rules:

* **The subject is the file's owner and the file's organization, never the actor.** An admin
  retrying a member's file neither launders the member's missing acceptance nor covers it with
  the admin's own.
* **Fail closed.** A configured key the active document provider does not serve refuses with
  ``legal_document_unavailable`` (a typo never disables the gate); a settings or acceptance
  read that raises refuses with ``legal_gate_unavailable``.
* A changed document text (new sha256) needs a fresh acceptance: matching is on the hash.

Every entry point that starts processing calls :func:`require_processing_allowed` (routes) or
:func:`check_processing_allowed` (tasks that record a refusal instead of raising), and
``tasks/transcription/dispatch._run_pre_dispatch_gate`` calls it FIRST as the backstop.
``tests/unit/test_processing_gate_coverage.py`` derives the entry points from the code and
fails on an unchecked one.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_LEGAL_PROCESSING_ACCEPTANCE_SCOPE
from app.core.constants import LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY
from app.core.constants import LEGAL_PROCESSING_ACCEPTANCE_SCOPES
from app.core.constants import LEGAL_PROCESSING_GATE_TTL_SECONDS
from app.core.constants import LEGAL_PROCESSING_REQUIRED_KEYS_KEY
from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance

logger = logging.getLogger(__name__)

REASON_ACCEPTANCE_REQUIRED = "legal_acceptance_required"
REASON_DOCUMENT_UNAVAILABLE = "legal_document_unavailable"
REASON_GATE_UNAVAILABLE = "legal_gate_unavailable"


@dataclass(frozen=True)
class ProcessingRefusal:
    """Why processing of a file was refused, and which documents are involved."""

    code: str
    document_keys: tuple[str, ...]

    @property
    def message(self) -> str:
        """A one-line, stable description stored as a file's last error."""
        return f"{self.code}: {', '.join(self.document_keys) or '-'}"


@dataclass(frozen=True)
class GateConfig:
    """The resolved settings. ``required_keys`` empty means the gate is off."""

    required_keys: tuple[str, ...]
    scope: str


_cache_lock = threading.Lock()
_cached: tuple[float, GateConfig] | None = None


def parse_required_keys(raw: str | None) -> tuple[str, ...]:
    """Split the stored comma-separated list; order-preserving, de-duplicated, no blanks."""
    seen: dict[str, None] = {}
    for part in (raw or "").split(","):
        key = part.strip()
        if key:
            seen.setdefault(key, None)
    return tuple(seen)


def invalidate_cache() -> None:
    """Forget the cached configuration (after a settings write, and in tests)."""
    global _cached
    with _cache_lock:
        _cached = None


def _cache_disabled() -> bool:
    # The same escape hatches as ``core/settings_cache``: TTL 0, and the test suite, where
    # settings rows live in a rolled-back savepoint and a cached answer would leak across tests.
    from app.core.config import settings

    return settings.SETTINGS_CACHE_TTL <= 0 or os.environ.get("TESTING", "").lower() == "true"


def read_config(db: Session) -> GateConfig:
    """The gate configuration, cached for ``LEGAL_PROCESSING_GATE_TTL_SECONDS``.

    A miss reads the DB in ONE query. An unknown stored scope reads as ``"user"``, the
    narrowest one, so a bad row can only make the gate stricter.

    Raises:
        Exception: Whatever the settings read raised; callers turn it into a refusal.
    """
    global _cached
    from app.services.system_settings_service import get_settings_map

    now = time.monotonic()
    use_cache = not _cache_disabled()
    with _cache_lock:
        if use_cache and _cached is not None and _cached[0] > now:
            return _cached[1]
    values = get_settings_map(
        db, [LEGAL_PROCESSING_REQUIRED_KEYS_KEY, LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY]
    )
    scope = (
        values.get(LEGAL_PROCESSING_ACCEPTANCE_SCOPE_KEY)
        or DEFAULT_LEGAL_PROCESSING_ACCEPTANCE_SCOPE
    )
    if scope not in LEGAL_PROCESSING_ACCEPTANCE_SCOPES:
        scope = "user"
    config = GateConfig(
        required_keys=parse_required_keys(values.get(LEGAL_PROCESSING_REQUIRED_KEYS_KEY)),
        scope=scope,
    )
    with _cache_lock:
        _cached = (now + LEGAL_PROCESSING_GATE_TTL_SECONDS, config)
    return config


def _user_accepted(db: Session, user_id: int, doc: registry.LegalDocument) -> bool:
    row = db.execute(
        select(LegalAcceptance.id)
        .where(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.document_key == doc.key,
            LegalAcceptance.sha256 == doc.sha256,
            *_user_scope_clause(),
        )
        .limit(1)
    ).first()
    return row is not None


def _user_scope_clause() -> list:
    # Organization-scope acceptances (an org admin accepting FOR the org) live in the same
    # table once its ``scope`` column exists; one of those is not the admin's personal one.
    scope_col = getattr(LegalAcceptance, "scope", None)
    return [] if scope_col is None else [scope_col == "user"]


def _org_accepted(db: Session, organization_id: int, doc: registry.LegalDocument) -> bool:
    scope_col = getattr(LegalAcceptance, "scope", None)
    org_col = getattr(LegalAcceptance, "organization_id", None)
    if scope_col is None or org_col is None:
        # This schema cannot record an organization acceptance, so none can satisfy the gate.
        return False
    row = db.execute(
        select(LegalAcceptance.id)
        .where(
            scope_col == "organization",
            org_col == organization_id,
            LegalAcceptance.document_key == doc.key,
            LegalAcceptance.sha256 == doc.sha256,
        )
        .limit(1)
    ).first()
    return row is not None


def _evaluate(
    db: Session, config: GateConfig, owner_user_id: int, organization_id: int | None
) -> ProcessingRefusal | None:
    docs: list[registry.LegalDocument] = []
    unavailable = []
    for key in config.required_keys:
        doc = registry.get_document(key)
        if doc is None:
            unavailable.append(key)
        else:
            docs.append(doc)
    if unavailable:
        return ProcessingRefusal(REASON_DOCUMENT_UNAVAILABLE, tuple(unavailable))

    missing = []
    for doc in docs:
        ok = False
        if config.scope in ("user", "either"):
            ok = _user_accepted(db, owner_user_id, doc)
        if not ok and config.scope in ("organization", "either") and organization_id is not None:
            ok = _org_accepted(db, organization_id, doc)
        if not ok:
            missing.append(doc.key)
    if missing:
        return ProcessingRefusal(REASON_ACCEPTANCE_REQUIRED, tuple(missing))
    return None


def check_processing_allowed(
    db: Session, *, owner_user_id: int, organization_id: int | None
) -> ProcessingRefusal | None:
    """``None`` when processing a file of *owner_user_id* in *organization_id* may start.

    Off (no required keys) costs the cached settings read and nothing else. Never raises: a
    failed read is the ``legal_gate_unavailable`` refusal, not an allowance.
    """
    try:
        config = read_config(db)
    except Exception:
        logger.exception("Legal processing gate: settings read failed; refusing")
        return ProcessingRefusal(REASON_GATE_UNAVAILABLE, ())
    if not config.required_keys:
        return None
    try:
        return _evaluate(db, config, int(owner_user_id), organization_id)
    except Exception:
        logger.exception("Legal processing gate: acceptance read failed; refusing")
        return ProcessingRefusal(REASON_GATE_UNAVAILABLE, config.required_keys)


def require_processing_allowed(
    db: Session, *, owner_user_id: int, organization_id: int | None
) -> None:
    """Raise ``LegalAcceptanceRequiredError`` when :func:`check_processing_allowed` refuses."""
    refusal = check_processing_allowed(
        db, owner_user_id=owner_user_id, organization_id=organization_id
    )
    if refusal is not None:
        raise refusal_error(refusal)


def require_processing_allowed_for_file(db: Session, media_file) -> None:
    """:func:`require_processing_allowed` for a ``MediaFile``'s owner and organization."""
    require_processing_allowed(
        db,
        owner_user_id=int(media_file.user_id),
        organization_id=media_file.organization_id,
    )


def check_processing_allowed_for_file(db: Session, media_file) -> ProcessingRefusal | None:
    """:func:`check_processing_allowed` for a ``MediaFile``'s owner and organization."""
    return check_processing_allowed(
        db,
        owner_user_id=int(media_file.user_id),
        organization_id=media_file.organization_id,
    )


def refusal_error(refusal: ProcessingRefusal):
    """The HTTP-shaped exception for *refusal* (403, or 503 when the gate cannot decide)."""
    from app.tasks.transcription.hooks import LegalAcceptanceRequiredError

    return LegalAcceptanceRequiredError(refusal.code, refusal.document_keys)


def mark_file_refused(db: Session, file_id: int, refusal: ProcessingRefusal) -> None:
    """Record *refusal* on a file that was already queued for processing, and commit.

    The file goes to ERROR with the refusal as its last error and a permanent error category,
    so automatic retry and recovery never pick it up again (they would only be refused again).
    """
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.utils.error_classification import ErrorCategory
    from app.utils.task_utils import update_media_file_status

    media_file = db.get(MediaFile, file_id)
    if media_file is None:
        return
    media_file.last_error_message = refusal.message
    media_file.error_category = ErrorCategory.LEGAL_ACCEPTANCE_REQUIRED.value
    update_media_file_status(db, file_id, FileStatus.ERROR)


def refusal_for_file(
    *, file_uuid: str | None = None, file_id: int | None = None
) -> ProcessingRefusal | None:
    """The gate's answer for one file, in a short session of its own (for task bodies).

    ``None`` for a file that does not exist: the task's own not-found handling applies.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    if (file_uuid is None) == (file_id is None):
        raise TypeError("pass exactly one of file_uuid= / file_id=")
    with session_scope() as db:
        query = db.query(MediaFile.user_id, MediaFile.organization_id)
        if file_id is not None:
            row = query.filter(MediaFile.id == file_id).first()
        else:
            row = query.filter(MediaFile.uuid == file_uuid).first()
        if row is None:
            return None
        return check_processing_allowed(db, owner_user_id=int(row[0]), organization_id=row[1])


def skipped_result(refusal: ProcessingRefusal) -> dict:
    """What a task returns when the gate refused it: skipped, never an error or a retry."""
    return {"status": "skipped", "reason": refusal.code, "documents": list(refusal.document_keys)}
