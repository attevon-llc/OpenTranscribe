"""Support-access grants and their use log (issue #1122).

In a multi-tenant deployment a platform admin holds no implicit access to tenant content.
Staff reach it only through a time-boxed, audited grant: requested with a reason and
approved by the tenant, or opened by a ``super_admin`` in an emergency with a reason and a
ticket. This revision adds the two tables behind it.

``support_access_grant``
    One row per request, approval or break-glass opening. Every foreign key is
    ``ON DELETE SET NULL``, never ``CASCADE``: a grant is the evidence that support touched
    a tenant, and cascading from the organization or the user would delete it exactly when
    the tenant or the account is erased. A grant whose target has been nulled is permanently
    unusable; the row stays. Status (pending, active, expired, ...) is computed from the
    timestamps, never stored.

``support_access_use``
    Append-only, ids only. ``organization_id`` and ``owner_user_id`` are snapshot stamps with
    deliberately NO foreign key (the ``erasure_ledger`` pattern) so they outlive the erasure
    of the rows they name. No filename, title or name is ever stored here.

Idempotency and the detection marker
------------------------------------
The startup runner stamps untracked databases by schema fingerprint, so this revision
routinely re-runs over its own partial output. Every statement is guarded. The CHECK that
the detection arm keys on, ``ck_support_access_grant_target_kind``, is created LAST: a
database interrupted part-way must not look finished.

Community edition: both tables stay empty and unreferenced. In single-tenant mode every
``/support-access`` route answers 404 and no code path writes either table.

Downgrade destroys the grant and use evidence. That is the correct mirror of the upgrade
(the same shape as ``v389_add_erasure_ledger``), and it means rolling back after any support
session has happened loses the record that it happened.

Revision ID: v432_support_access_grant
Revises: v431_add_media_duration_provenance
Create Date: 2026-10-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v432_support_access_grant"
down_revision = "v431_add_media_duration_provenance"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements rather than asserting
#: on this file's source text (the convention v387/v388 established).
CREATE_GRANT_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS support_access_grant (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL,
        target_kind VARCHAR(20) NOT NULL,
        organization_id INTEGER REFERENCES organization(id) ON DELETE SET NULL,
        subject_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        grantee_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        access_level VARCHAR(10) NOT NULL,
        grant_mode VARCHAR(20) NOT NULL,
        reason TEXT NOT NULL,
        ticket_ref VARCHAR(255),
        requested_duration_minutes INTEGER NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        decided_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        decided_at TIMESTAMPTZ,
        decision VARCHAR(10),
        starts_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        revoked_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL
    );
"""

CREATE_USE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS support_access_use (
        id BIGSERIAL PRIMARY KEY,
        grant_id INTEGER NOT NULL
            REFERENCES support_access_grant(id) ON DELETE RESTRICT,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        method VARCHAR(10) NOT NULL,
        route VARCHAR(255) NOT NULL,
        resource_type VARCHAR(40),
        resource_uuid UUID,
        need VARCHAR(5),
        organization_id INTEGER,
        owner_user_id INTEGER
    );
"""

CREATE_INDEXES_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_support_access_grant_uuid
        ON support_access_grant (uuid);
    CREATE INDEX IF NOT EXISTS ix_support_access_grant_grantee_expires
        ON support_access_grant (grantee_user_id, expires_at);
    CREATE INDEX IF NOT EXISTS ix_support_access_grant_org_requested
        ON support_access_grant (organization_id, requested_at);
    CREATE INDEX IF NOT EXISTS ix_support_access_grant_subject_requested
        ON support_access_grant (subject_user_id, requested_at);
    CREATE INDEX IF NOT EXISTS ix_support_access_use_grant_occurred
        ON support_access_use (grant_id, occurred_at);
"""

#: (constraint name, table, CHECK body). Added one DO block each so a partial earlier run
#: can complete. ``target_kind`` is LAST: it is the detection marker.
CHECKS = (
    (
        "ck_support_access_grant_org_target_no_subject",
        "support_access_grant",
        "NOT (target_kind = 'organization' AND subject_user_id IS NOT NULL)",
    ),
    (
        "ck_support_access_grant_personal_target_no_org",
        "support_access_grant",
        "NOT (target_kind = 'personal' AND organization_id IS NOT NULL)",
    ),
    (
        "ck_support_access_grant_access_level",
        "support_access_grant",
        "access_level IN ('read', 'write')",
    ),
    (
        "ck_support_access_grant_grant_mode",
        "support_access_grant",
        "grant_mode IN ('approved', 'break_glass')",
    ),
    (
        "ck_support_access_grant_decision",
        "support_access_grant",
        "decision IS NULL OR decision IN ('approved', 'denied')",
    ),
    (
        "ck_support_access_grant_duration",
        "support_access_grant",
        "requested_duration_minutes BETWEEN 15 AND 480",
    ),
    (
        "ck_support_access_grant_break_glass",
        "support_access_grant",
        "grant_mode <> 'break_glass' OR "
        "(ticket_ref IS NOT NULL AND requested_duration_minutes <= 240)",
    ),
    (
        "ck_support_access_grant_reason_length",
        "support_access_grant",
        "char_length(reason) BETWEEN 10 AND 2000",
    ),
    (
        "ck_support_access_grant_window",
        "support_access_grant",
        "expires_at IS NULL OR expires_at > starts_at",
    ),
    (
        "ck_support_access_use_need",
        "support_access_use",
        "need IS NULL OR need IN ('read', 'write')",
    ),
    (
        "ck_support_access_grant_target_kind",
        "support_access_grant",
        "target_kind IN ('organization', 'personal')",
    ),
)


def _add_check_sql(name: str, table: str, body: str) -> str:
    return f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
            ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({body});
        END IF;
    END $$;
    """


UPGRADE_SQL = (
    CREATE_GRANT_TABLE_SQL
    + CREATE_USE_TABLE_SQL
    + CREATE_INDEXES_SQL
    + "".join(_add_check_sql(*check) for check in CHECKS)
)

#: Uses first: ``support_access_use.grant_id`` is RESTRICT.
DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS support_access_use;
    DROP TABLE IF EXISTS support_access_grant;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
