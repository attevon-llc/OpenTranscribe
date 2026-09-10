"""One throwaway PostgreSQL container per pytest session, shared by the docker-exec-only suites.

Why
---
Three suites (``test_opentr_restore_roundtrip``, ``test_dbs_wait_for_speaker_attributes``,
``test_dbs_wait_for_table_settled``) each stood up their **own** Postgres container **per
test** -- 11 containers for 11 tests. Measured on this host, one such container cost ~124 s
(``docker run`` 26-34 s, ``initdb`` 62-92 s, ``docker rm -f`` 17-22 s), so the three suites
spent ~1,360 s of the integration phase's 3,781 s wall clock provisioning databases rather
than testing anything. The full measurement table is in ``throwaway_pg.py``'s docstring.

Two fixes, both waste-only:

* ``--tmpfs`` on ``PGDATA`` (in ``throwaway_pg.start_container``) takes ``initdb`` from
  62-92 s to 4.6 s and ``CREATE DATABASE`` from 7.1 s to 0.12 s.
* This file: pay ``docker run``/``docker rm`` **once per session** instead of once per test.

What is preserved, and how
--------------------------
Every test still gets a cluster in which the only databases are the ones it creates itself.
``isolated_pg`` snapshots ``pg_database`` before the test and drops everything new afterwards
(``WITH (FORCE)``, so a test that deliberately holds a connection open cannot leave residue).
For anything DATABASE-scoped -- ``CREATE``/``DROP DATABASE``, ``pg_restore``, corrupting
``alembic_version`` on purpose -- that is the same guarantee a private container gave, because
none of those operations reach outside the database they name.

What deliberately does NOT share this container
-----------------------------------------------
``test_cleanup_test_data_isolated.py`` and ``test_cleanup_test_users_isolated_db.py`` keep
their own **module-scoped** containers. They connect over **TCP** (SQLAlchemy/psycopg against
a loopback-published port), and a published port is incompatible with the ``--network none``
posture the exec-only suites rely on to guarantee the container cannot reach the live stack.
Weakening that for three suites to save one container start on two others is the wrong trade.
Those two already pay their container once per module (2 containers for 11 tests) and already
use ``--tmpfs``, so there is little left to remove there anyway.

If a suite ever needs to restart the *server*, or to exercise cluster-level state (roles,
``ALTER SYSTEM``, ``pg_hba``), it must NOT use ``isolated_pg`` -- give it a private container
and say why. Nothing in the three suites does today; that was checked, not assumed.

Under ``pytest -n auto`` each xdist worker is its own process and therefore gets its own
session container (uuid4-suffixed name, so no collision) and its own per-test database names.
The bound is the number of workers that actually run one of these 14 tests, so at most 14
containers -- alive concurrently rather than one at a time, but each is a ~50 MB tmpfs cluster
and they start in parallel, which is faster in wall clock, not slower.

The gate runs this phase **serially**: ``run-integration-tests.sh`` passes ``-o addopts=""``,
which drops pyproject's ``-n auto`` along with everything else in ``addopts``. Measured on the
2026-09-07 gate's own ``integration.xml``: 177 testcases summing to 3,780.5 s against a
3,781 s phase wall clock -- sum equals wall clock, which is only possible with one worker. So
in the gate there is exactly one container here.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator

import pytest

from tests.integration import throwaway_pg


@pytest.fixture(scope="session")
def shared_pg_container() -> Iterator[str]:
    """A single network-isolated Postgres container for the whole session.

    Prefer ``isolated_pg`` in tests: it adds the per-test cleanup that makes sharing safe.
    Depend on this directly only if you genuinely want to observe the cluster across tests.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    name = f"ot-shared-throwaway-pg-{uuid.uuid4().hex[:12]}"
    # Fresh per session and not a credential anyone relies on: the container is
    # `--network none`, so the only way in is our own `docker exec`, which authenticates
    # over the local Unix socket rather than with this password.
    throwaway_pg.start_container(name, password=uuid.uuid4().hex)
    try:
        throwaway_pg.wait_ready(name)
        yield name
    finally:
        throwaway_pg.remove_container(name)


@pytest.fixture
def isolated_pg(shared_pg_container: str) -> Iterator[str]:
    """The shared container, with every database the test created dropped afterwards.

    Yields the container name, so a test body reads exactly as it did when this was a
    private per-test container.
    """
    baseline = throwaway_pg.list_databases(shared_pg_container)
    try:
        yield shared_pg_container
    finally:
        throwaway_pg.drop_databases_outside(shared_pg_container, baseline)
