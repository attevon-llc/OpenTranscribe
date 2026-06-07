"""
API endpoints for custom vocabulary management.

Custom vocabulary provides domain-specific keyword boosting for ASR providers that
support it (e.g. Deepgram, AssemblyAI, Speechmatics) as well as faster-whisper
hotword injection for local transcription.

Supported domains: medical, legal, corporate, government, technical, general
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.utils.uuid_helpers import require_resource_owner

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

SUPPORTED_DOMAINS = [
    "medical",
    "legal",
    "corporate",
    "government",
    "technical",
    "general",
]

# Hard limits enforced at the API layer (database column sizes match these).
_MAX_TERM_LEN = 200
_MAX_CATEGORY_LEN = 100
_MAX_BULK_IMPORT_ITEMS = 1000


# ---------------------------------------------------------------------------
# Lazy model import helper
# ---------------------------------------------------------------------------


def _get_vocab_model():
    """Lazily import CustomVocabulary model to avoid early import errors."""
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    return CustomVocabulary


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def _term_to_dict(term: Any) -> dict:
    """Serialize a CustomVocabulary row to a public dict."""
    return {
        "id": term.id,
        "user_id": term.user_id,
        "term": term.term,
        "domain": term.domain,
        "category": term.category,
        "is_active": term.is_active,
        "is_system": term.user_id is None,
        "created_at": term.created_at.isoformat() if term.created_at else None,
        "updated_at": term.updated_at.isoformat() if term.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/domains")
def list_domains(
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Return the list of supported vocabulary domains."""
    return {"domains": SUPPORTED_DOMAINS}


@router.get("")
def list_vocabulary(
    domain: str | None = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    List vocabulary terms visible to the current user.

    Returns both the user's own terms and system-wide terms (user_id IS NULL).
    Optionally filter by domain.
    """
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    # User's own terms
    user_q = db.query(CustomVocabulary).filter(CustomVocabulary.user_id == current_user.id)
    # System terms (shared, read-only for non-admins)
    system_q = db.query(CustomVocabulary).filter(CustomVocabulary.user_id.is_(None))

    if domain:
        if domain not in SUPPORTED_DOMAINS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain '{domain}'. Supported: {', '.join(SUPPORTED_DOMAINS)}",
            )
        user_q = user_q.filter(CustomVocabulary.domain == domain)
        system_q = system_q.filter(CustomVocabulary.domain == domain)

    if active_only:
        user_q = user_q.filter(CustomVocabulary.is_active.is_(True))
        system_q = system_q.filter(CustomVocabulary.is_active.is_(True))

    user_terms = user_q.order_by(CustomVocabulary.domain, CustomVocabulary.term).all()
    system_terms = system_q.order_by(CustomVocabulary.domain, CustomVocabulary.term).all()

    return {
        "terms": [_term_to_dict(t) for t in user_terms],
        "system_terms": [_term_to_dict(t) for t in system_terms],
        "total": len(user_terms),
        "total_system": len(system_terms),
    }


@router.post("", status_code=201)
def create_vocabulary_term(
    body: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Create a new custom vocabulary term for the current user."""
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    term_text = (body.get("term") or "").strip()
    if not term_text:
        raise HTTPException(status_code=400, detail="'term' is required and must not be empty")
    if len(term_text) > _MAX_TERM_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"'term' must not exceed {_MAX_TERM_LEN} characters",
        )

    category = body.get("category")
    if category and len(str(category)) > _MAX_CATEGORY_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"'category' must not exceed {_MAX_CATEGORY_LEN} characters",
        )

    domain = body.get("domain", "general")
    if domain not in SUPPORTED_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain '{domain}'. Supported: {', '.join(SUPPORTED_DOMAINS)}",
        )

    # Duplicate check (per-user + domain)
    existing = (
        db.query(CustomVocabulary)
        .filter(
            CustomVocabulary.user_id == current_user.id,
            CustomVocabulary.term == term_text,
            CustomVocabulary.domain == domain,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Term '{term_text}' already exists in domain '{domain}'",
        )

    term = CustomVocabulary(
        user_id=current_user.id,
        term=term_text,
        domain=domain,
        category=category,
        is_active=body.get("is_active", True),
    )
    db.add(term)
    db.commit()
    db.refresh(term)

    return _term_to_dict(term)


@router.put("/{term_id}")
def update_vocabulary_term(  # noqa: C901
    term_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Update a custom vocabulary term (users can only modify their own terms)."""
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    term = db.query(CustomVocabulary).filter(CustomVocabulary.id == term_id).first()
    if not term:
        raise HTTPException(status_code=404, detail="Vocabulary term not found")

    # Ownership check: system terms (user_id=None) are read-only for non-admins
    if term.user_id is None:
        raise HTTPException(
            status_code=403,
            detail="System vocabulary terms are read-only",
        )
    require_resource_owner(
        term, current_user, forbidden_detail="Not authorized to modify this vocabulary term"
    )

    if "term" in body:
        new_text = (body["term"] or "").strip()
        if not new_text:
            raise HTTPException(status_code=400, detail="'term' must not be empty")
        if len(new_text) > _MAX_TERM_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"'term' must not exceed {_MAX_TERM_LEN} characters",
            )
        term.term = new_text  # type: ignore[assignment]

    if "domain" in body:
        new_domain = body["domain"]
        if new_domain not in SUPPORTED_DOMAINS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain '{new_domain}'. Supported: {', '.join(SUPPORTED_DOMAINS)}",
            )
        term.domain = new_domain  # type: ignore[assignment]

    if "category" in body:
        new_cat = body["category"]
        if new_cat and len(str(new_cat)) > _MAX_CATEGORY_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"'category' must not exceed {_MAX_CATEGORY_LEN} characters",
            )
        term.category = new_cat  # type: ignore[assignment]

    if "is_active" in body:
        term.is_active = bool(body["is_active"])  # type: ignore[assignment]

    db.add(term)
    db.commit()
    db.refresh(term)

    return _term_to_dict(term)


@router.delete("/all", status_code=204)
def delete_all_user_vocabulary(
    domain: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    """Delete all custom vocabulary terms for the current user (optionally filter by domain)."""
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    q = db.query(CustomVocabulary).filter(CustomVocabulary.user_id == current_user.id)
    if domain:
        if domain not in SUPPORTED_DOMAINS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain '{domain}'. Supported: {', '.join(SUPPORTED_DOMAINS)}",
            )
        q = q.filter(CustomVocabulary.domain == domain)

    q.delete()
    db.commit()


@router.delete("/{term_id}", status_code=204)
def delete_vocabulary_term(
    term_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    """Delete a custom vocabulary term (users can only delete their own terms)."""
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    term = db.query(CustomVocabulary).filter(CustomVocabulary.id == term_id).first()
    if not term:
        raise HTTPException(status_code=404, detail="Vocabulary term not found")

    if term.user_id is None:
        raise HTTPException(
            status_code=403,
            detail="System vocabulary terms cannot be deleted",
        )
    require_resource_owner(
        term, current_user, forbidden_detail="Not authorized to delete this vocabulary term"
    )

    db.delete(term)
    db.commit()


@router.post("/bulk", status_code=201)
def bulk_import_vocabulary(  # noqa: C901
    body: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Bulk import vocabulary terms.

    Expected body: { "terms": [ {"term": str, "domain": str, "category": str|null}, ... ] }

    Duplicate terms (same term + domain for this user) are silently skipped.
    Returns counts of created and skipped terms.
    """
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    raw_terms = body.get("terms", [])
    if not isinstance(raw_terms, list):
        raise HTTPException(status_code=400, detail="'terms' must be a list")
    if len(raw_terms) > _MAX_BULK_IMPORT_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"Bulk import is limited to {_MAX_BULK_IMPORT_ITEMS} terms per request",
        )

    created = 0
    skipped = 0
    errors: list[str] = []

    # First pass: validate all items and collect the unique (term, domain) pairs to insert.
    validated: list[dict] = []
    for idx, item in enumerate(raw_terms):
        if not isinstance(item, dict):
            errors.append(f"Item {idx}: must be an object with 'term' field")
            skipped += 1
            continue

        term_text = (item.get("term") or "").strip()
        if not term_text:
            errors.append(f"Item {idx}: 'term' is required and must not be empty")
            skipped += 1
            continue
        if len(term_text) > _MAX_TERM_LEN:
            errors.append(f"Item {idx}: 'term' exceeds {_MAX_TERM_LEN} characters")
            skipped += 1
            continue

        category = item.get("category")
        if category and len(str(category)) > _MAX_CATEGORY_LEN:
            errors.append(
                f"Item {idx} ('{term_text}'): 'category' exceeds {_MAX_CATEGORY_LEN} characters"
            )
            skipped += 1
            continue

        domain = item.get("domain", "general")
        if domain not in SUPPORTED_DOMAINS:
            errors.append(f"Item {idx} ('{term_text}'): invalid domain '{domain}'")
            skipped += 1
            continue

        validated.append(
            {
                "term": term_text,
                "domain": domain,
                "category": category,
                "is_active": item.get("is_active", True),
            }
        )

    if not validated:
        return {
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "message": f"Imported {created} terms, skipped {skipped}",
        }

    # Single query to fetch all existing (term, domain) pairs for this user.
    # Avoids one DB round-trip per validated term (N+1 → 1 query).
    existing_pairs: set[tuple[str, str]] = {
        (row.term, row.domain)
        for row in db.query(CustomVocabulary.term, CustomVocabulary.domain)
        .filter(CustomVocabulary.user_id == current_user.id)
        .all()
    }

    for item in validated:
        key = (item["term"], item["domain"])
        if key in existing_pairs:
            skipped += 1
            continue
        # Track in-memory so duplicate entries within the same import batch are also skipped.
        existing_pairs.add(key)
        db.add(
            CustomVocabulary(
                user_id=current_user.id,
                term=item["term"],
                domain=item["domain"],
                category=item["category"],
                is_active=item["is_active"],
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "message": f"Imported {created} terms, skipped {skipped}",
    }


@router.get("/export")
def export_vocabulary(
    domain: str | None = None,
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Export user's custom vocabulary terms as JSON."""
    from app.models.custom_vocabulary import CustomVocabulary  # type: ignore[import]

    q = db.query(CustomVocabulary).filter(CustomVocabulary.user_id == current_user.id)

    if domain:
        if domain not in SUPPORTED_DOMAINS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain '{domain}'. Supported: {', '.join(SUPPORTED_DOMAINS)}",
            )
        q = q.filter(CustomVocabulary.domain == domain)

    if active_only:
        q = q.filter(CustomVocabulary.is_active.is_(True))

    terms = q.order_by(CustomVocabulary.domain, CustomVocabulary.term).all()

    export_data = {
        "terms": [
            {
                "term": t.term,
                "domain": t.domain,
                "category": t.category,
                "is_active": t.is_active,
            }
            for t in terms
        ],
        "total": len(terms),
        "exported_by": current_user.email,
    }

    return JSONResponse(
        content=export_data,
        headers={
            "Content-Disposition": "attachment; filename=custom_vocabulary.json",
        },
    )
