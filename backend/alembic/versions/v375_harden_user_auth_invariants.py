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

Third — the same invariant seen from the provisioning side. Disabling
self-registration (``allow_registration``, #354) is only safe if there is a
working admin-driven way to create an account, and there was not:
``POST /api/admin/users`` made the admin type a password, sent no mail, stamped
no ``password_changed_at``, wrote no password-history row, and could not set
``auth_type`` at all — so every admin-created account was ``local`` and could not
log in on a deployment where local passwords are off. This revision adds the
schema the invitation flow needs:

``user_invitation``
    One hashed, expiring, single-use invite per row (same shape as
    ``password_reset_token``), carrying the *target* ``role`` and ``auth_type``
    so an LDAP/OIDC/PKI account can be pre-provisioned and matched at first
    login. Both are CHECK-constrained against the same closed sets as ``user``,
    for the same reason: an invitation is a promise about a row that does not
    exist yet, and an out-of-set value there would only be caught after the
    account was created.

``user.email_verified`` / ``email_verified_at`` and ``email_verification_token``
    ``require_email_verification`` has been a declared auth-config key with no
    reader anywhere — the setting existed, the feature did not. These columns are
    what makes it enforceable. Note ``external_identity.email_verified`` is a
    DIFFERENT flag (IdP-asserted, consumed by ``app/auth/external_sync.py``);
    this one records that *we* proved control of the address.

Existing accounts are grandfathered to verified when the column is first added:
they were provisioned before the feature existed, and an admin turning the
setting on must not lock out the entire deployment retroactively. That backfill
runs only inside the ADD COLUMN guard, so re-running the revision never
re-verifies an address an admin deliberately marked unverified.

COMMUNITY EDITION: no behavioural change to existing rows. A correctly-migrated
database has no NULL roles and no unknown auth types, so both backfills are
no-ops; the new tables are empty until an admin sends an invitation.

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

#: Admin provisioning (invitations) + email verification. Both tables mirror
#: ``password_reset_token``: a SHA-256 token hash — never the token — plus
#: ``expires_at`` and ``used_at`` for single-use semantics.
INVITATION_DDL = """
    CREATE TABLE IF NOT EXISTS user_invitation (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL UNIQUE,
        email VARCHAR(255) NOT NULL,
        full_name VARCHAR(255),
        role VARCHAR(20) NOT NULL DEFAULT 'user',
        auth_type VARCHAR(20) NOT NULL DEFAULT 'local',
        token_hash VARCHAR(64) NOT NULL UNIQUE,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        used_at TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        created_by_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        created_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        ip_address VARCHAR(45),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        CONSTRAINT ck_user_invitation_role_valid
            CHECK (role IN ('user', 'admin', 'super_admin')),
        CONSTRAINT ck_user_invitation_auth_type_valid
            CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'))
    );
    CREATE INDEX IF NOT EXISTS idx_user_invitation_email ON user_invitation (email);
    CREATE INDEX IF NOT EXISTS idx_user_invitation_created_by
        ON user_invitation (created_by_id);

    CREATE TABLE IF NOT EXISTS email_verification_token (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        token_hash VARCHAR(64) NOT NULL UNIQUE,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        used_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        ip_address VARCHAR(45)
    );
    CREATE INDEX IF NOT EXISTS idx_email_verification_token_user
        ON email_verification_token (user_id);
"""

#: The grandfather UPDATE lives INSIDE the ADD COLUMN guard: on a re-run the
#: column already exists, so an address an admin deliberately un-verified is
#: never silently re-verified.
EMAIL_VERIFIED_COLUMNS_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'email_verified'
        ) THEN
            ALTER TABLE "user"
                ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT FALSE;
            UPDATE "user" SET email_verified = TRUE;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'email_verified_at'
        ) THEN
            ALTER TABLE "user"
                ADD COLUMN email_verified_at TIMESTAMP WITH TIME ZONE;
            UPDATE "user" SET email_verified_at = now() WHERE email_verified;
        END IF;
    END $$;
"""


#: Password expiry (``PASSWORD_MAX_AGE_DAYS``) keys off ``password_changed_at``,
#: and no account-creation path ever stamped it — so on an upgrade every existing
#: local account has NULL and the control stays inert.
#:
#: Stamped to **now**, deliberately, not to ``created_at``. We do not know when
#: these passwords were actually last changed; dating them from account creation
#: would immediately expire most of a deployment on upgrade day and confine every
#: user to the change-password screen at once. Starting the clock at upgrade makes
#: the control real going forward without manufacturing a support incident.
#:
#: Local accounts only: the column is meaningless for LDAP/OIDC/PKI identities,
#: whose password lives with the provider, and stamping it would make the admin
#: account-status report count them as expiring.
PASSWORD_CHANGED_AT_BACKFILL_SQL = """
    UPDATE "user"
       SET password_changed_at = now()
     WHERE password_changed_at IS NULL
       AND auth_type = 'local';
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

    # One of the two markers this revision is detected by (app/db/migrations.py);
    # the other is the user_invitation table created below.
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

    op.execute(EMAIL_VERIFIED_COLUMNS_SQL)
    op.execute(INVITATION_DDL)
    op.execute(PASSWORD_CHANGED_AT_BACKFILL_SQL)


def downgrade():
    op.execute("DROP TABLE IF EXISTS user_invitation")
    op.execute("DROP TABLE IF EXISTS email_verification_token")
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS email_verified_at')
    op.execute('ALTER TABLE "user" DROP COLUMN IF EXISTS email_verified')
    op.execute('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid')
    # Deliberately partial: the columns are NOT NULL in the model and in
    # database/init_db.sql, so re-widening them would put the schema back into the
    # state this revision exists to make impossible. The constraint drop is enough
    # to let an older application version run.
