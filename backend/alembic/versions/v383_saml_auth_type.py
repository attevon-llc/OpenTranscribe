"""Add ``saml`` to the auth-type CHECK constraints and the SAML identity column.

Companion to ``v380`` for a fourth external identity source (#35). SAML slots in as
``auth_type='saml'`` reusing the whole policy layer already built for OIDC — admission
control, approval state, ``account_linking``, sessions, audit — so the marginal schema
cost is exactly this: one CHECK value and one identity column.

``user.saml_subject`` mirrors ``user.oidc_subject`` deliberately, down to the same
single-provider caveat: it stores the assertion's ``NameID``, which is unique **per
IdP entity**, not globally. The UNIQUE index here is sound only while exactly one SAML
IdP is configured — the same limitation ``oidc_subject`` documents, carried forward
rather than solved differently for a second protocol.

COMMUNITY EDITION: a deployment that never configures SAML has no ``'saml'`` rows;
the CHECK swap and column add are pure DDL and the new column starts NULL on every
existing row.

Revision ID: v383_saml_auth_type
Revises: v382_scim_tokens
Create Date: 2026-08-07
"""

from alembic import op

revision = "v383_saml_auth_type"
down_revision = "v382_scim_tokens"
branch_labels = None
depends_on = None

#: The auth types the database accepts after this revision. A superset of
#: ``app.auth.constants.VALID_AUTH_TYPES`` by the same design v380 established.
VALID_AUTH_TYPES_SQL = "'local', 'ldap', 'oidc', 'pki', 'proxy', 'saml'"
PREVIOUS_AUTH_TYPES_SQL = "'local', 'ldap', 'oidc', 'pki', 'proxy'"

UPGRADE_SQL = f"""
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid;
    ALTER TABLE "user" ADD CONSTRAINT ck_user_auth_type_valid
        CHECK (auth_type IN ({VALID_AUTH_TYPES_SQL}));

    ALTER TABLE user_invitation
        DROP CONSTRAINT IF EXISTS ck_user_invitation_auth_type_valid;
    ALTER TABLE user_invitation ADD CONSTRAINT ck_user_invitation_auth_type_valid
        CHECK (auth_type IN ({VALID_AUTH_TYPES_SQL}));

    ALTER TABLE "user" ADD COLUMN IF NOT EXISTS saml_subject VARCHAR(255);
    CREATE UNIQUE INDEX IF NOT EXISTS ix_user_saml_subject
        ON "user" (saml_subject);
"""

DOWNGRADE_SQL = f"""
    -- Any 'saml' rows are demoted to 'local' first — the v377 precedent for a value
    -- the shrinking constraint no longer accepts. A downgrade that instead refused
    -- to run would strand every SAML user mid-rollback with no clean recovery path.
    UPDATE "user" SET auth_type = 'local' WHERE auth_type = 'saml';
    UPDATE user_invitation SET auth_type = 'local' WHERE auth_type = 'saml';

    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid;
    ALTER TABLE "user" ADD CONSTRAINT ck_user_auth_type_valid
        CHECK (auth_type IN ({PREVIOUS_AUTH_TYPES_SQL}));

    ALTER TABLE user_invitation
        DROP CONSTRAINT IF EXISTS ck_user_invitation_auth_type_valid;
    ALTER TABLE user_invitation ADD CONSTRAINT ck_user_invitation_auth_type_valid
        CHECK (auth_type IN ({PREVIOUS_AUTH_TYPES_SQL}));

    DROP INDEX IF EXISTS ix_user_saml_subject;
    ALTER TABLE "user" DROP COLUMN IF EXISTS saml_subject;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
