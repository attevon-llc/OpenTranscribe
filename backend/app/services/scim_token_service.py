"""Issue, verify and revoke the bearer tokens that authenticate ``/scim/v2/*``.

The single owner of the SCIM credential's lifecycle. Three properties it exists to
guarantee:

* **The secret is never stored.** ``issue_token`` returns the plaintext exactly once
  and persists only its SHA-256 digest, which is also the lookup key — verification
  is one indexed equality, not a scan-and-compare over every row.
* **Verification is total.** Revoked and expired are checked on the same read that
  finds the row, so there is no path that resolves a token without applying them.
* **Revocation is one-way.** ``revoked_at`` is set once; nothing here clears it. A
  provisioning credential that can be un-revoked is a credential whose revocation
  you cannot reason about.

SHA-256 rather than a password hash: the token is 256 bits of ``secrets`` output, so
there is no dictionary to slow down, and a per-request bcrypt on a bulk provisioning
endpoint is a self-inflicted throughput problem. Same reasoning — and the same
construction — as ``models/invitation.py``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.scim_token import SCIMToken

logger = logging.getLogger(__name__)

#: Prefix on the plaintext, so a leaked string is identifiable at a glance and so a
#: future credential type can be told apart without trying it.
TOKEN_PREFIX = "ot_scim_"  # noqa: S105 # nosec B105

#: Bytes of entropy behind the random half. 32 bytes = 256 bits.
TOKEN_ENTROPY_BYTES = 32


def hash_token(raw_token: str) -> str:
    """Return the stored digest of a presented token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Mint a new plaintext token. Never stored, never logged."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)}"


def issue_token(
    db: Session,
    *,
    name: str,
    created_by: int | None,
    expires_at: datetime | None = None,
) -> tuple[SCIMToken, str]:
    """Create a token row and return it with its one-time plaintext.

    Args:
        db: Session; committed here so the caller can hand the secret straight back.
        name: Operator-facing label ("Okta production").
        created_by: The issuing super_admin's id.
        expires_at: Optional expiry. ``None`` means "until revoked".

    Returns:
        ``(row, plaintext)``. The plaintext is the only copy that will ever exist.
    """
    raw = generate_token()
    row = SCIMToken(
        name=name.strip(),
        token_hash=hash_token(raw),
        created_by=created_by,
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Issued SCIM token %r (uuid=%s)", row.name, row.uuid)
    return row, raw


def verify_token(db: Session, raw_token: str | None) -> SCIMToken | None:
    """Resolve a presented bearer token to a usable row, or ``None``.

    Stamps ``last_used_at`` on success so an integration nobody is using is visible
    in the admin list.

    Args:
        db: Session.
        raw_token: The value from the ``Authorization: Bearer`` header.

    Returns:
        The token row when it exists, is not revoked and is not expired.
    """
    if not raw_token:
        return None

    row = db.query(SCIMToken).filter(SCIMToken.token_hash == hash_token(raw_token)).first()
    if row is None:
        return None
    if row.revoked_at is not None:
        logger.warning("Rejected a revoked SCIM token (uuid=%s)", row.uuid)
        return None

    expires_at = row.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) >= expires_at:
            logger.warning("Rejected an expired SCIM token (uuid=%s)", row.uuid)
            return None

    row.last_used_at = datetime.now(UTC)  # type: ignore[assignment]
    db.commit()
    return row


def revoke_token(db: Session, token_uuid: str) -> SCIMToken | None:
    """Revoke a token by UUID. Idempotent; never un-revokes.

    Returns:
        The row, or ``None`` when no such token exists.
    """
    row = db.query(SCIMToken).filter(SCIMToken.uuid == token_uuid).first()
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)  # type: ignore[assignment]
        db.commit()
        db.refresh(row)
        logger.info("Revoked SCIM token %r (uuid=%s)", row.name, row.uuid)
    return row


def list_tokens(db: Session) -> list[SCIMToken]:
    """Every token, newest first. Revoked rows are included — see the model."""
    return db.query(SCIMToken).order_by(SCIMToken.id.desc()).all()
