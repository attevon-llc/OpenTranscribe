"""The schema-detection ladder: every probe's answer, and every arm newest first.

``run_migrations()`` stamps an untracked database at the FIRST arm of ``LADDER`` whose
predicate holds, then lets Alembic apply everything above it. Stamping too high skips DDL the
database never received, so the order of ``LADDER`` is the contract:

* ``collect_facts`` runs every probe of every era, oldest era first, before any arm is
  evaluated: the ladder's queries and their order do not depend on which arm matches.
* ``LADDER`` is the eras' ``ARMS`` slices concatenated newest era first. Within an era the
  newest revision comes first.

A new revision gets its probe in the newest era's ``probe()`` (and a field on its facts
class) and its arm at the TOP of that era's ``ARMS``; ``tests/unit/test_schema_detection_ladder.py``
pins every arm's position.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.db.schema_detect import auth_identity
from app.db.schema_detect import cloud_seams
from app.db.schema_detect import governance
from app.db.schema_detect import ingest_tenancy
from app.db.schema_detect import legacy
from app.db.schema_detect.auth_identity import AuthIdentityFacts
from app.db.schema_detect.cloud_seams import CloudSeamsFacts
from app.db.schema_detect.governance import GovernanceFacts
from app.db.schema_detect.ingest_tenancy import IngestTenancyFacts
from app.db.schema_detect.legacy import LegacyFacts

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class SchemaFacts:
    """Every probe's answer for one database, grouped by era."""

    tables: tuple[str, ...]
    legacy: LegacyFacts
    seams: CloudSeamsFacts
    auth: AuthIdentityFacts
    tenancy: IngestTenancyFacts
    governance: GovernanceFacts


#: One ladder entry: a predicate over the facts, and the revision to stamp when it holds.
Arm = tuple[Callable[[SchemaFacts], bool], str]


def collect_facts(conn: Connection, tables: list[str]) -> SchemaFacts:
    """Run every era's probes against *conn*, oldest era first (keyword order is call order)."""
    legacy_facts = legacy.probe(conn, tables)
    return SchemaFacts(
        tables=tuple(tables),
        legacy=legacy_facts,
        seams=cloud_seams.probe(conn, tables),
        auth=auth_identity.probe(conn, tables, has_auth_config=legacy_facts.has_auth_config),
        tenancy=ingest_tenancy.probe(conn, tables),
        governance=governance.probe(conn, tables),
    )


LADDER: tuple[Arm, ...] = (
    governance.ARMS + ingest_tenancy.ARMS + auth_identity.ARMS + cloud_seams.ARMS + legacy.ARMS
)
