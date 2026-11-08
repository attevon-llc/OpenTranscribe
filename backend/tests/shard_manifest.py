"""Write the node ids this pytest run selected to ``$OT_SHARD_MANIFEST`` (CI sharding proof).

Loaded with ``-p tests.shard_manifest``. Each CI shard (``--splits N --group i``) and the one
unsplit collection job write a manifest; ``scripts/guards/check_shard_coverage.py`` then fails
unless the shards' manifests are disjoint and their union equals the unsplit one, so a test
can neither be run twice nor silently dropped by the split. Inert when the variable is unset.

Under xdist every worker collects the same selection, so only ``gw0`` (or a non-xdist run)
writes, and the file is replaced atomically.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

MANIFEST_ENV = "OT_SHARD_MANIFEST"


def pytest_collection_finish(session: pytest.Session) -> None:
    target = os.environ.get(MANIFEST_ENV)
    if not target:
        return
    worker = getattr(session.config, "workerinput", None)
    if worker is not None and worker.get("workerid") != "gw0":
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text("".join(f"{item.nodeid}\n" for item in session.items), encoding="utf-8")
    scratch.replace(path)
