"""The erasure store registry stays complete (issue #1353, plan test R1/R2).

The receipt only proves what its registry can count. A table added next year that holds a
subject's rows, or an OpenSearch index nobody classified, is exactly the store an erasure
forgets, so the completeness is DERIVED from the live schema and cluster instead of remembered.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.services.erasure_receipt.inventory import STORES
from app.services.erasure_receipt.inventory import register_erasure_store
from app.services.erasure_receipt.stores_external import classify_index
from app.services.erasure_receipt.stores_pg import EXEMPT_TABLES
from app.services.erasure_receipt.stores_pg import PG_STORE_TABLES
from app.services.erasure_receipt.stores_pg import PG_STORES
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope

#: The tables whose foreign keys, at any depth and under any delete rule, reach a subject.
_ROOTS = ("user", "media_file", "speaker", "organization")

_CLOSURE_SQL = """
    WITH RECURSIVE edges AS (
        SELECT replace(c.confrelid::regclass::text, '"', '') AS parent,
               replace(c.conrelid::regclass::text, '"', '') AS child
        FROM pg_constraint c WHERE c.contype = 'f'
    ), walk(tbl) AS (
        SELECT unnest(CAST(:roots AS text[]))
        UNION
        SELECT e.child FROM edges e JOIN walk w ON e.parent = w.tbl
    )
    SELECT tbl FROM walk
"""


def _closure(db_session) -> set[str]:
    roots = "{" + ",".join(_ROOTS) + "}"
    rows = db_session.connection().execute(text(_CLOSURE_SQL), {"roots": roots}).all()
    return {r[0] for r in rows} - set(_ROOTS)


def test_every_table_reachable_from_a_subject_is_classified(db_session):
    reachable = _closure(db_session)
    # The deriving query matching nothing would pass everything below.
    assert len(reachable) >= 30, f"the foreign-key closure found only {len(reachable)} tables"

    classified = PG_STORE_TABLES | set(EXEMPT_TABLES) | set(_ROOTS)
    missing = sorted(reachable - classified)
    assert not missing, (
        f"tables reachable from a data subject with no erasure store and no exemption: {missing}. "
        "Add a PgStore in services/erasure_receipt/stores_pg.py, or an EXEMPT_TABLES entry with "
        "the reason it needs no count of its own."
    )


def test_no_exemption_names_a_table_that_is_gone_or_already_counted(db_session):
    reachable = _closure(db_session)
    stale = sorted(set(EXEMPT_TABLES) - reachable)
    assert not stale, f"EXEMPT_TABLES entries that are not reachable any more: {stale}"
    both = sorted(set(EXEMPT_TABLES) & PG_STORE_TABLES)
    assert not both, f"tables that are both counted and exempt: {both}"
    assert all(reason.strip() for reason in EXEMPT_TABLES.values())


def test_store_keys_are_unique_and_logical():
    keys = [s.key for s in STORES]
    assert len(keys) == len(set(keys))
    import re

    shape = re.compile(r"^(pg|os|s3|redis|fs|mirror|backup)[.][a-z0-9_.]{1,80}$")
    assert all(shape.match(k) for k in keys), [k for k in keys if not shape.match(k)]


def test_a_store_cannot_be_registered_twice():
    with pytest.raises(ValueError):
        register_erasure_store(STORES[0])


_SCOPES = {
    "user": ErasureScope("user", user_id=-1),
    "org_member": ErasureScope("org_member", user_id=-1, organization_id=-1),
    "organization": ErasureScope("organization", organization_id=-1),
    "file": ErasureScope("file", file_ids=(-1,)),
}


@pytest.mark.parametrize("store", PG_STORES, ids=lambda s: s.key)
def test_every_postgres_selector_is_valid_sql_for_every_scope_it_names(db_session, store):
    """A selector naming a column that does not exist would read as an unmeasurable store at
    run time. Run each one for real, against ids that match nothing."""
    counter = next(s for s in STORES if s.key == store.key).count
    assert store.where, "a store that applies to no scope counts nothing"
    for kind in store.where:
        assert counter(_SCOPES[kind], Captured(), db_session) == 0


def test_every_postgres_store_has_a_reason_when_it_keeps_data():
    keeping = [s for s in PG_STORES if s.disposition in ("retained", "de_attributed")]
    assert len(keeping) >= 4, "the stores that keep data on purpose are not being found"
    unexplained = [s.key for s in keeping if not s.reason]
    assert unexplained == [], f"{unexplained} keep data without saying why"


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("transcripts", "os.transcripts"),
        ("transcript_chunks", "os.transcript_chunks"),
        ("transcript_chunks_v6", "os.transcript_chunks"),
        ("transcript_summaries", "os.transcript_summaries"),
        ("speakers_v4", "os.speakers"),
        ("speakers_v3_backup", "os.speakers"),
        ("audit-logs-2026.11", "os.audit"),
        ("documents_v1", None),
        ("ot-test-stray", None),
    ],
)
def test_index_classification(name, key):
    assert classify_index(name) == key


def _sweep():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "test_artifact_sweep.py"
    spec = importlib.util.spec_from_file_location("test_artifact_sweep_shapes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    os.environ.get("SKIP_OPENSEARCH") == "True", reason="needs a reachable OpenSearch"
)
def test_every_live_index_is_classified_or_a_system_index():
    from app.services.erasure_receipt.stores_external import unclassified_indices

    # Indices a concurrent test run is minting right now carry a registered test-artifact shape
    # (the sweep deletes them); they are that run's scratch, not a store an erasure forgets. A
    # real application index, or a test index of an UNREGISTERED shape, still fails here.
    sweep = _sweep()
    stray = [
        n for n in unclassified_indices() if sweep.match_spec(n, sweep.TEST_INDEX_SPECS) is None
    ]
    assert stray == [], (
        f"OpenSearch indices no erasure store covers: {stray}. Classify them in "
        "stores_external.INDEX_PATTERNS, or (an index with no personal data) allow-list them."
    )
