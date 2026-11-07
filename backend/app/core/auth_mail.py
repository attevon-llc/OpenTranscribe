"""Dispatch of credential-bearing auth mail to its background tasks.

Callers in ``app.auth`` and ``app.services`` queue mail by task name, the repo's late
binding for Celery tasks, so the request path never imports the task layer. The tasks
live in ``app/tasks/auth_mail_tasks.py``; their queue is set in ``core/celery_routing.py``.
Arguments are ids and plain values only: a single-use token is minted inside the task.
"""

from __future__ import annotations

from app.core.celery import celery_app

RESET_MAIL_TASK = "auth_mail.password_reset"
EMAIL_VERIFICATION_TASK = "auth_mail.email_verification"
INVITATION_TASK = "auth_mail.invitation"
SECURITY_NOTICE_TASK = "auth_mail.security_notice"


def dispatch_auth_mail(task_name: str, *args: int | str | None) -> None:
    """Queue one auth-mail task. Raises if the broker refuses the message."""
    celery_app.send_task(task_name, args=list(args))
