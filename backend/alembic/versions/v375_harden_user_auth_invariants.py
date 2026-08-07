"""Close the two holes in the user table's authorization invariants.

``v369_superuser_role_invariant`` added the CHECK constraints that make
``is_superuser`` a derived mirror of ``role == 'super_admin'``::

    ck_user_role_valid              CHECK (role IN ('user','admin','super_admin'))
    ck_user_superuser_matches_role  CHECK (is_superuser = (role = 'super_admin'))

but ``user.role`` is still NULLABLE in the DDL (it has been since
``v010_baseline``), and in PostgreSQL a CHECK that evaluates to UNKNOWN
**passes**. With ``role = NULL`` both constraints evaluate to UNKNOWN, so a row
can carry ``is_superuser = TRUE`` with no role at all and satisfy the very
constraint that exists to prevent it. v369's own reconciliation
(``WHERE is_superuser <> (role = 'super_admin')``) skips those rows for the same
reason. Authorization itself fails closed — every gate compares ``role`` against
a literal, and NULL matches none of them — so this is an integrity hole rather
than a live escalation, but the invariant the model and CLAUDE.md advertise is
not actually enforced until the column is NOT NULL.

Second: ``user.auth_type`` has no CHECK at all, although
``app/auth/constants.py:VALID_AUTH_TYPES`` has always been the closed set. That
matters because ``api/endpoints/auth/mfa_tokens.py`` treats an ``auth_type``
outside the known set as "this user cannot enrol in local MFA" — so a typo'd or
injected value silently exempts an account from MFA rather than failing loudly.

Both changes are backfill-then-constrain and safe to re-run. Rows with a NULL
role are repaired to ``'user'`` (the least-privilege value, and the column's own
server default) with ``is_superuser`` re-derived, before the NOT NULL lands;
rows with an unrecognised ``auth_type`` are repaired to ``'local'``. Neither
backfill can grant privilege: it only ever removes it.

COMMUNITY EDITION: no behavioural change. A correctly-migrated database has no
NULL roles and no unknown auth types, so both backfills are no-ops and only the
constraints are added.

Revision ID: v375_harden_user_auth_invariants
Revises: v374_add_tag_user_id
Create Date: 2026-08-07
"""

from alembic import op

revision = "v375_harden_user_auth_invariants"
down_revision = "v374_add_tag_user_id"
branch_labels = None
depends_on = None

#: Repairs that must run before the constraints can be added. Module-level so the
#: consistency test can replay it against deliberately-broken seeded rows.
BACKFILL_SQL = """
    -- A NULL role defeats BOTH v369 CHECKs (NULL comparisons are UNKNOWN, and
    -- UNKNOWN passes a CHECK). Demote to the least-privilege value rather than
    -- guessing, and re-derive the mirror.
    UPDATE "user" SET role = 'user' WHERE role IS NULL;
    UPDATE "user" SET is_superuser = (role = 'super_admin')
     WHERE is_superuser IS DISTINCT FROM (role = 'super_admin');

    -- An unrecognised auth_type silently exempts the account from local MFA
    -- enrolment. 'local' is the model default and the safe interpretation.
    UPDATE "user" SET auth_type = 'local'
     WHERE auth_type IS NULL
        OR auth_type NOT IN ('local', 'ldap', 'keycloak', 'pki');
"""


def upgrade():
    op.execute(BACKFILL_SQL)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'user'
                  AND column_name = 'role'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE "user" ALTER COLUMN role SET NOT NULL;
            END IF;
        END $$;
    """)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'user'
                  AND column_name = 'auth_type'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE "user" ALTER COLUMN auth_type SET NOT NULL;
            END IF;
        END $$;
    """)

    # The marker this revision is detected by (app/db/migrations.py).
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_auth_type_valid'
            ) THEN
                ALTER TABLE "user"
                    ADD CONSTRAINT ck_user_auth_type_valid
                    CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'));
            END IF;
        END $$;
    """)


def downgrade():
    op.execute("ALTER TABLE \"user\" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid")
    # Deliberately partial: the columns are NOT NULL in the model and in
    # database/init_db.sql, so re-widening them would put the schema back into the
    # state this revision exists to make impossible. The constraint drop is enough
    # to let an older application version run.
