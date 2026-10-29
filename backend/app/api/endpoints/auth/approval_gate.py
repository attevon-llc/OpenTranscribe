"""HTTP-layer answer to "is a pending account refused right now?"."""

from fastapi import HTTPException
from fastapi import status

from app.auth import approval


def approval_enforced(db) -> bool:
    """Whether a pending account is refused right now.

    Raises:
        HTTPException: 503 with a generic body when the setting cannot be read.
            The cause is logged by :func:`app.auth.approval.approval_required`, never returned.
    """
    required = approval.approval_required(db)
    if required is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "auth_config_unavailable",
                "message": "Authentication settings are temporarily unavailable.",
            },
        )
    return required
