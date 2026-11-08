"""``org_content_transfer.restamp``: point a transferred member's search documents at the new owner.

Dispatched after the Postgres transfer commits (``services/org_content_transfer``). Retries are
Celery's documented automatic-retry options (Celery docs, "Tasks > Retrying" and "Automatic retry
for known exceptions"): ``autoretry_for`` the OpenSearch transport errors and the "left documents
behind" signal, exponential ``retry_backoff`` with jitter, bounded ``max_retries``, and
``acks_late`` so a worker lost mid-run gets the message redelivered. The work is idempotent
(documents already on the new owner are not selected), so every one of those is safe.
"""

from __future__ import annotations

from typing import Any

from opensearchpy.exceptions import OpenSearchException

from app.core.celery import celery_app
from app.core.queues import UtilityPriority
from app.services.org_content_transfer import RESTAMP_TASK
from app.services.org_content_transfer import RestampIncompleteError
from app.services.org_content_transfer import restamp_documents


@celery_app.task(
    name=RESTAMP_TASK,
    priority=UtilityPriority.ROUTINE,
    autoretry_for=(OpenSearchException, RestampIncompleteError),
    retry_backoff=30,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=8,
    acks_late=True,
    reject_on_worker_lost=True,
)
def restamp(
    to_user_id: int,
    file_ids: list[int],
    profile_ids: list[int],
    cluster_uuids: list[str],
) -> dict[str, Any]:
    """Restamp ``user_id`` on the moved ids' documents, then recompute their access lists."""
    updated = restamp_documents(
        to_user_id=to_user_id,
        file_ids=file_ids,
        profile_ids=profile_ids,
        cluster_uuids=cluster_uuids,
    )
    if file_ids:
        celery_app.send_task("update_file_access_index", args=[file_ids])
    return {"updated": updated}
