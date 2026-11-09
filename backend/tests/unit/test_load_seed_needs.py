"""The scale seed gives every load-test persona the data its tasks read and edit.

``scripts/perf/load/routes.py`` lists, per persona, what its tasks need (``SEED_NEEDS``: files
with segments, comments, personal tags, collections, cached waveforms, chat history, holds, an
organization admin...). This seeds a small shape with the flags the runbook passes and checks
each need for the persona's own block of identities, so a seed change that starves a persona
fails here instead of turning a load run into empty pages.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.scripts.scale_dataset import pg_synthetic as pg
from app.scripts.scale_dataset.pg_governance import seed_all
from tests.fixtures import locust_stub as stub

# Seeds write fixed natural keys (perf-N@loadtest.invalid) into uncommitted rows, so two of
# these open on different workers block on each other's unique index while each still holds the
# shared DDL lock, which stalls every ddl_exclusive test queued behind them. One group, one worker.
pytestmark = pytest.mark.xdist_group("scale_dataset_seed")

SEEDED_USERS = 24
# A small copy of the runbook's load seed: same flags, scaled down.
LOAD_SHAPE = pg.Shape(
    users=SEEDED_USERS,
    files=600,
    heavy_user_files=120,
    speakers_per_file=3,
    segments_per_file=4,
    tasks_per_file=1,
    profiles=60,
    tags=10,
    tags_per_file=2,
    orgs=2,
    holds=4,
    hold_files=10,
    collections=60,
    collection_members=200,
    collection_shares=4,
    clusters=4,
    cluster_members=8,
    matches=10,
    hidden=6,
    conversations=60,
    messages=240,
    comments=600,
    user_tags=100,
    waveform_pm=1000,
)


@pytest.fixture
def load(monkeypatch):
    return stub.load_modules(monkeypatch)


def unmet_needs(conn, scenario, routes) -> list[str]:
    """Every ``SEED_NEEDS`` entry that counts zero rows on the seeded database."""
    unmet = []
    for persona, (pool, needs) in routes.SEED_NEEDS.items():
        lo, hi = (
            scenario.pool_bounds(pool, SEEDED_USERS)
            if pool in dict(scenario.POOL_SHARES)
            else (0, 0)
        )
        for label, sql in needs:
            binds = {k: v for k, v in (("lo", lo), ("hi", hi)) if f":{k}" in sql}
            if not conn.execute(text(sql), binds).scalar_one():
                unmet.append(f"{persona}: {label}")
    return unmet


def _seed(db_session, shape: pg.Shape) -> None:
    seed_all(db_session.connection(), shape, "needs1337", "x-not-a-real-hash")


def test_every_persona_has_its_needs_listed(load):
    assert set(load["routes"].SEED_NEEDS) == set(load["locustfile"].USER_CLASSES)


def test_the_load_seed_meets_every_personas_needs(db_session, load):
    _seed(db_session, LOAD_SHAPE)

    assert unmet_needs(db_session.connection(), load["scenario"], load["routes"]) == []


def test_a_seed_without_comments_or_waveforms_starves_the_viewer(db_session, load):
    fields = {**LOAD_SHAPE.__dict__, "comments": 0, "waveform_pm": 0}
    _seed(db_session, pg.Shape(**fields))

    unmet = unmet_needs(db_session.connection(), load["scenario"], load["routes"])

    assert unmet == ["Viewer: comments on own files", "Viewer: cached waveforms on own files"]


def test_the_runbook_seeds_with_the_flags_the_needs_assume(load):
    readme = (stub.LOAD_DIR / "README.md").read_text()

    assert "--waveform-pm 1000" in readme
