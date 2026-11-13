"""Make the speakers page render real cluster rows, from media the suite owns.

The ``speakers`` visual surface masks ``.last-clustered-chip`` and ``.member-count``. Both
are rendered only when the account has clusters and a clustering run has been recorded
(``ClustersTab.svelte``: ``{#if lastClusteredAt}``; ``ClusterHeaderChips.svelte``), so on an
account that owns no media the masks match nothing and ``_assert_masks`` rightly refuses to
capture. The data is produced the way a user produces it: the owned clip's speakers go
through the real ``POST /api/speaker-clusters/recluster`` task.
"""

from __future__ import annotations

import time

import pytest
import requests


def wait_for_clustered_speakers(backend_url: str, token: str, timeout_secs: int = 180) -> None:
    """Run a recluster and wait until a cluster page reports a cluster and a run time.

    Fails (never skips) with the last page it saw, so an empty result names the cause.
    """
    headers = {"Authorization": f"Bearer {token}"}
    started = requests.post(
        f"{backend_url}/api/speaker-clusters/recluster", headers=headers, json={}, timeout=60
    )
    assert started.status_code == 200, (
        f"recluster refused: {started.status_code} {started.text[:300]}"
    )
    deadline = time.time() + timeout_secs
    page: dict = {}
    while time.time() < deadline:
        response = requests.get(f"{backend_url}/api/speaker-clusters", headers=headers, timeout=30)
        if response.ok:
            page = response.json()
            if page.get("items") and page.get("last_clustered_at"):
                return
        time.sleep(2)
    pytest.fail(
        "no speaker cluster with a recorded clustering run appeared within "
        f"{timeout_secs}s of POST /speaker-clusters/recluster; last page: "
        f"total={page.get('total')!r}, last_clustered_at={page.get('last_clustered_at')!r}"
    )
