"""Let directory groups drive in-app groups and privileges.

Both directory paths already extract the caller's full group list and then throw
almost all of it away. ``auth/ldap_auth.py`` builds ``user_groups`` and puts it in
``LdapUserData.groups``; ``auth/keycloak_auth.py`` reads the configurable roles
claim into ``KeycloakUserData.roles``. In both cases exactly one bit survived —
``is_admin`` — so ``CN=Legal-Team,OU=Groups,DC=corp,DC=example`` could never become
the OpenTranscribe sharing group "Legal". ``user_group`` / ``user_group_member``
existed the whole time and **no auth code referenced either**, so teams had to be
rebuilt by hand and drifted from the directory immediately.

This revision adds the two pieces of schema that close that gap.

``group_mapping``
    One directory claim value bound to a ``user_group`` and/or a granted role.
    Both halves are optional, but a mapping granting neither does nothing, so
    ``ck_group_mapping_grants_something`` rejects it.

    ``grants_role`` is capped at ``admin`` by ``ck_group_mapping_role_capped``.
    ``super_admin`` must stay unreachable from any IdP: it is the break-glass
    account for the very directory that might be failing, and the whole
    ``role``/``is_superuser`` invariant (``v369``, hardened in ``v375``) exists to
    keep that one privilege local. The service layer enforces the same cap, but the
    CHECK is what makes it true of the database rather than of one code path.

    Uniqueness is ``(source, claim_value)``: one claim value resolves to at most one
    mapping, so "what does this user get?" has exactly one answer per group.
    ``uq_group_mapping_ldap_claim_ci`` adds the case-insensitive half for LDAP only —
    DNs are case-insensitive and the existing membership check
    (``ldap_auth._is_member_of_groups``) already compares them lowercased, so two
    rows differing only in case would both match one directory group. OIDC role
    strings ARE case-sensitive, so the index is partial rather than global.

``user_group_member.source``
    ``manual`` (the default, and what every pre-existing row becomes) or the
    directory that produced the row. Without it, reconciliation has only two
    possible behaviours and both are wrong: never revoke, or revoke everything
    including memberships an admin added by hand. With it, a pass removes only what
    a directory pass created. The column is NOT NULL with a server default so the
    backfill is the default itself — existing behaviour is preserved exactly.

COMMUNITY EDITION: no behavioural change until an admin creates a mapping. With
zero ``group_mapping`` rows, reconciliation resolves an empty grant set, adds
nothing, and removes nothing (there are no directory-sourced memberships to
remove). Legacy ``ldap_admin_groups`` / ``keycloak_admin_role`` promotion is
unaffected and keeps working alongside mappings.

Revision ID: v376_idp_group_mapping
Revises: v375_harden_user_auth_invariants
Create Date: 2026-08-07
"""

from alembic import op

revision = "v376_idp_group_mapping"
down_revision = "v375_harden_user_auth_invariants"
branch_labels = None
depends_on = None

#: The mapping table. Module-level so the consistency test can assert the CHECK
#: text without re-deriving it. ``ON DELETE CASCADE`` on ``user_group_id``:
#: deleting the target group takes the mapping with it rather than silently leaving
#: a role-only grant that no longer appears anywhere in the groups UI.
GROUP_MAPPING_DDL = """
    CREATE TABLE IF NOT EXISTS group_mapping (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL UNIQUE,
        source VARCHAR(20) NOT NULL,
        claim_value VARCHAR(1024) NOT NULL,
        user_group_id INTEGER REFERENCES user_group(id) ON DELETE CASCADE,
        grants_role VARCHAR(20),
        description TEXT,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        CONSTRAINT uq_group_mapping_source_claim UNIQUE (source, claim_value),
        CONSTRAINT ck_group_mapping_source_valid
            CHECK (source IN ('ldap', 'oidc')),
        CONSTRAINT ck_group_mapping_role_capped
            CHECK (grants_role IS NULL OR grants_role IN ('user', 'admin')),
        CONSTRAINT ck_group_mapping_grants_something
            CHECK (user_group_id IS NOT NULL OR grants_role IS NOT NULL)
    );
    CREATE INDEX IF NOT EXISTS idx_group_mapping_source ON group_mapping (source);
    CREATE INDEX IF NOT EXISTS idx_group_mapping_user_group
        ON group_mapping (user_group_id);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_group_mapping_ldap_claim_ci
        ON group_mapping (lower(claim_value)) WHERE source = 'ldap';
"""

#: The provenance column. NOT NULL DEFAULT 'manual' is the backfill: every row that
#: existed before this revision was put there by a human through the groups UI, and
#: reconciliation must never touch it.
MEMBERSHIP_SOURCE_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user_group_member' AND column_name = 'source'
        ) THEN
            ALTER TABLE user_group_member
                ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'manual';
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_user_group_member_source_valid'
        ) THEN
            ALTER TABLE user_group_member
                ADD CONSTRAINT ck_user_group_member_source_valid
                CHECK (source IN ('manual', 'ldap', 'oidc'));
        END IF;
    END $$;

    -- Reconciliation's hot query is "this user's directory-derived rows".
    CREATE INDEX IF NOT EXISTS idx_user_group_member_user_source
        ON user_group_member (user_id, source);
"""


def upgrade():
    op.execute(GROUP_MAPPING_DDL)
    op.execute(MEMBERSHIP_SOURCE_SQL)


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_user_group_member_user_source")
    op.execute(
        "ALTER TABLE user_group_member DROP CONSTRAINT IF EXISTS ck_user_group_member_source_valid"
    )
    op.execute("ALTER TABLE user_group_member DROP COLUMN IF EXISTS source")
    op.execute("DROP TABLE IF EXISTS group_mapping")
