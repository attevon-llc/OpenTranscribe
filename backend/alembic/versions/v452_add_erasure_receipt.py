"""Add ``erasure_receipt`` and the ``file`` erasure subject (#1353).

An erasure already recorded THAT it was asked for (``erasure_ledger``) but not WHAT was
destroyed in each store, nor whether anything was left. The receipt is that measurement: one
row per measurement, counts keyed by logical store name (``pg.media_file``, ``os.transcripts``,
``s3.media.file_objects``...), and a sha256 digest of the canonical data fields.

Like the ledger it must not contain the personal data it records the destruction of: there is
no free-text column, every map is JSONB under two CHECKs (each value a JSON number, each key a
logical store name), so a filename, an address or a physical bucket name cannot be stored even
by a caller that tries. ``ledger_entry_id`` is ``ON DELETE RESTRICT``: a ledger entry cannot
vanish under its receipts.

The same revision adds ``erasure_ledger.subject_file_uuid`` and widens the two ledger CHECKs
for a ``file`` subject (one recording erased on request, Art. 17 on a single file).

All SQL is idempotent. The downgrade DROPS every receipt (compliance evidence; the journal's
receipt lines survive) and removes ``file`` entries, so it is only for a database that never
issued one; it is not executed by its test.
"""

from alembic import op

revision = "v452_add_erasure_receipt"
down_revision = "v451_legal_acceptance_survives_user_deletion"
branch_labels = None
depends_on = None

_MAPS = ("deleted", "de_attributed", "retained", "ttl", "residual", "unverified")

#: Written the way Postgres stores a jsonpath (no space before the filter), see
#: ``models/erasure.COUNTERS_NUMERIC_SQL``.
_KEYS_SQL = (
    "NOT jsonb_path_exists({col}, "
    '\'$.keyvalue()?(!(@."key" like_regex "^(pg|os|s3|redis|fs|mirror|backup)[.][a-z0-9_.]{{1,80}}$"))\')'
)
_NUMERIC_SQL = "NOT jsonb_path_exists({col}, '$.*?(@.type() != \"number\")')"


def _add_check(name: str, body: str) -> str:
    return f"""
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
            ALTER TABLE erasure_receipt ADD CONSTRAINT {name} CHECK ({body});
        END IF;"""


_MAP_COLUMNS_SQL = ",\n        ".join(f"{c} JSONB NOT NULL DEFAULT '{{}}'::jsonb" for c in _MAPS)

CREATE_TABLE_SQL = f"""
    CREATE TABLE IF NOT EXISTS erasure_receipt (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL UNIQUE,
        ledger_entry_id INTEGER NOT NULL
            REFERENCES erasure_ledger(id) ON DELETE RESTRICT,
        attempt INTEGER NOT NULL,
        phase VARCHAR(12) NOT NULL,
        generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        outcome VARCHAR(12) NOT NULL,
        {_MAP_COLUMNS_SQL},
        inventory_version INTEGER NOT NULL,
        digest VARCHAR(64) NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ix_erasure_receipt_uuid ON erasure_receipt (uuid);
    CREATE INDEX IF NOT EXISTS ix_erasure_receipt_ledger_entry_id
        ON erasure_receipt (ledger_entry_id);
"""

#: Created in this order; the detection arm keys on the LAST one.
_CHECKS = (
    ("ck_erasure_receipt_phase", "phase IN ('immediate', 'delayed', 'resurrection')"),
    ("ck_erasure_receipt_outcome", "outcome IN ('verified', 'residual', 'unverified')"),
    *((f"ck_erasure_receipt_{c}_numeric", _NUMERIC_SQL.format(col=c)) for c in _MAPS),
    *((f"ck_erasure_receipt_{c}_keys", _KEYS_SQL.format(col=c)) for c in _MAPS),
    ("ck_erasure_receipt_digest", "digest ~ '^[0-9a-f]{64}$'"),
)

ADD_CHECKS_SQL = "DO $$ BEGIN" + "".join(_add_check(n, b) for n, b in _CHECKS) + "\nEND $$;"

#: The ledger learns a ``file`` subject. The two CHECKs are replaced (not added to) so their
#: text matches ``models/erasure`` exactly.
WIDEN_LEDGER_SQL = """
    ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS subject_file_uuid UUID;
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_subject;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_subject
        CHECK (subject_type IN ('user', 'org_member', 'organization', 'file'));
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_subject_identified;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_subject_identified
        CHECK (
            (subject_type = 'user' AND subject_user_id IS NOT NULL)
            OR (subject_type = 'organization' AND subject_organization_id IS NOT NULL)
            OR (subject_type = 'org_member' AND subject_user_id IS NOT NULL
                AND subject_organization_id IS NOT NULL)
            OR (subject_type = 'file' AND subject_file_uuid IS NOT NULL)
        );
"""

UPGRADE_SQL = WIDEN_LEDGER_SQL + CREATE_TABLE_SQL + ADD_CHECKS_SQL

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS erasure_receipt;
    DELETE FROM erasure_ledger WHERE subject_type = 'file';
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_subject;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_subject
        CHECK (subject_type IN ('user', 'org_member', 'organization'));
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_subject_identified;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_subject_identified
        CHECK (
            (subject_type = 'user' AND subject_user_id IS NOT NULL)
            OR (subject_type = 'organization' AND subject_organization_id IS NOT NULL)
            OR (subject_type = 'org_member' AND subject_user_id IS NOT NULL
                AND subject_organization_id IS NOT NULL)
        );
    ALTER TABLE erasure_ledger DROP COLUMN IF EXISTS subject_file_uuid;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
