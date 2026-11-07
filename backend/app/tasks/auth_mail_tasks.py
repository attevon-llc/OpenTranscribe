"""Credential-bearing auth mail, delivered off the request path.

Password reset, address verification, invitation and security-notice mail used to be sent
inside the HTTP request, and only when the account existed. The request therefore took
longer for an existing address than for an unknown one, so its duration told a caller
which addresses have an account. Each endpoint now does the same work for every address
(look the account up, dispatch one of these tasks) and everything that differs, including
the SMTP round trip, happens here.

Rules the tasks keep:

- **No credential in a task argument.** Arguments are ids; the single-use token is created
  inside the task and goes only into the message.
- **Transient transport failures retry** with exponential backoff and a bounded count
  (Celery ``autoretry_for`` / ``retry_backoff`` / ``max_retries``). A final failure is
  audited or logged by the module that owns the message, not raised.
- Queue routing lives in ``core/celery_routing.py``; none is set here.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.auth_mail import EMAIL_VERIFICATION_TASK
from app.core.auth_mail import INVITATION_TASK
from app.core.auth_mail import RESET_MAIL_TASK
from app.core.auth_mail import SECURITY_NOTICE_TASK
from app.core.celery import celery_app
from app.core.exceptions import EmailDeliveryError
from app.core.exceptions import EmailTransientError
from app.core.queues import UtilityPriority
from app.db.session_utils import session_scope

logger = logging.getLogger(__name__)

MAX_RETRIES = 4

_RETRY: dict[str, Any] = {
    "autoretry_for": (EmailTransientError,),
    "retry_backoff": 30,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": MAX_RETRIES,
}


def _is_final_attempt(task: Any) -> bool:
    return int(task.request.retries) >= int(task.max_retries)


@celery_app.task(name=RESET_MAIL_TASK, bind=True, priority=UtilityPriority.OPERATIONAL, **_RETRY)
def send_password_reset_mail(self: Any, user_id: int | None, ip_address: str) -> None:
    """Issue and mail a password-reset link for ``user_id`` (``None`` = no such account)."""
    from app.auth.password_reset import deliver_password_reset

    with session_scope() as db:
        deliver_password_reset(db, user_id, ip_address, final_attempt=_is_final_attempt(self))


@celery_app.task(
    name=EMAIL_VERIFICATION_TASK,
    bind=True,
    priority=UtilityPriority.OPERATIONAL,
    **_RETRY,
)
def send_email_verification_mail(self: Any, user_id: int | None, ip_address: str) -> None:
    """Issue and mail an address-verification link for ``user_id`` (``None`` = no account)."""
    from app.auth.email_verification import deliver_verification

    with session_scope() as db:
        deliver_verification(db, user_id, ip_address, final_attempt=_is_final_attempt(self))


@celery_app.task(name=INVITATION_TASK, bind=True, priority=UtilityPriority.OPERATIONAL, **_RETRY)
def send_invitation_mail(self: Any, invitation_id: int, expires_in_hours: int) -> None:
    """Mint the single-use link for an invitation row and mail it."""
    from app.auth.invitations import deliver_invitation

    with session_scope() as db:
        deliver_invitation(
            db, invitation_id, expires_in_hours, final_attempt=_is_final_attempt(self)
        )


@celery_app.task(
    name=SECURITY_NOTICE_TASK, bind=True, priority=UtilityPriority.OPERATIONAL, **_RETRY
)
def send_security_notice_mail(self: Any, to_email: str, subject: str, message: str) -> None:
    """Tell an account owner a security-relevant change was made to the account."""
    from app.services.email_service import email_service

    try:
        email_service.send_security_notice(to_email, subject, message)
    except EmailTransientError:
        if not _is_final_attempt(self):
            raise
        logger.error("Security notice could not be delivered after %d retries", MAX_RETRIES)
    except EmailDeliveryError:
        logger.error("Security notice could not be delivered")
