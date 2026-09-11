"""The single most important test in this fix — #891's exact scenario, closed.

``dispatch_reindex_for_every_owner`` (``services/search/model_switch.py``) used to
interpolate a caught Celery/Redis dispatch exception's raw text directly into the
message on ``ReindexDispatchError``. That exception's text can quote the broker
connection URL, credential embedded: ``redis://:<password>@host:port/db`` — a
message that then propagated, unmasked, all the way to an admin's browser via
``POST /api/search/reindex``'s 503 response.

Both the raw function call AND the real HTTP route are exercised here, because the
route is a second place the same fix could have been undone (e.g. by an endpoint
re-adding ``f"...{e}..."`` around the already-sanitized exception).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.services.search import model_switch

#: A broker URL with an embedded credential — the exact shape #891 leaked.
BROKER = "redis://:hunter2@redis-internal:6379/0"


def _raise_with_broker_url(**kwargs):
    raise RuntimeError(
        f"Retry limit exceeded while trying to reconnect to the Celery result "
        f"store backend: {BROKER}"
    )


def test_dispatch_reindex_for_every_owner_never_leaks_the_broker_credential(caplog):
    """Direct call: the raised message carries progress facts, never the credential."""
    with (
        patch(
            "app.tasks.reindex_task.reindex_transcripts_task.delay",
            side_effect=_raise_with_broker_url,
        ),
        caplog.at_level(logging.ERROR),
    ):
        with pytest.raises(model_switch.ReindexDispatchError) as exc_info:
            model_switch.dispatch_reindex_for_every_owner(
                triggered_by=1,
                file_uuids_by_owner={1: ["11111111-1111-1111-1111-111111111111"]},
            )

    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "redis://" not in message
    # The progress facts (how many of how many users) survive the fix.
    assert "0 of 1" in message

    # The real cause is still diagnosable via the log.
    assert "hunter2" in caplog.text


def test_reindex_http_route_never_leaks_the_broker_credential(client, admin_token_headers, caplog):
    """Same failure, driven through the real HTTP route (POST /api/search/reindex).

    No file_uuids/pending_only means the corpus-wide fan-out, which always
    dispatches the calling admin first (see the module docstring) — so the very
    first ``.delay()`` call already raises, regardless of what else the shared
    dev-stack database happens to hold.
    """
    with (
        patch(
            "app.tasks.reindex_task.reindex_transcripts_task.delay",
            side_effect=_raise_with_broker_url,
        ),
        caplog.at_level(logging.ERROR),
    ):
        response = client.post("/api/search/reindex", headers=admin_token_headers)

    assert response.status_code == 503
    body_text = response.text
    assert "hunter2" not in body_text
    assert "redis://" not in body_text
    assert "hunter2" in caplog.text
