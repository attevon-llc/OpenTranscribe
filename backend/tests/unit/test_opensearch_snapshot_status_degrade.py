"""``opensearch_snapshot.snapshot_status`` documents "never raises".

A non-404 error from the repository lookup (timeout, security-plugin refusal) after a
successful ping must not fail the admin Backup panel.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opensearchpy.exceptions import AuthorizationException
from opensearchpy.exceptions import ConnectionTimeout

from app.services import opensearch_snapshot


@pytest.mark.parametrize(
    "failure",
    [ConnectionTimeout("N/A", "timed out", Exception()), AuthorizationException(403, "no", {})],
)
def test_snapshot_status_survives_a_failing_repository_lookup(monkeypatch, failure):
    client = MagicMock()
    client.snapshot.get_repository.side_effect = failure
    client.snapshot.get.return_value = {"snapshots": []}
    monkeypatch.setattr(opensearch_snapshot, "_client", lambda: client)
    monkeypatch.setattr(opensearch_snapshot, "is_reachable", lambda c=None: True)

    status = opensearch_snapshot.snapshot_status()

    assert status["reachable"] is True
    assert status["repository_registered"] is False
