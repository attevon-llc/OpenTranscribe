"""SCIM 2.0 bearer tokens, and the ``proxy``/``scim`` group-source values.

Two changes ship together because the two features they belong to ship together, and
because the alternative is swapping the same two CHECK constraints twice in one
release. ``v380`` set the precedent deliberately: it pre-authorised ``'proxy'`` in
``ck_user_auth_type_valid`` so trusted-header authentication would need no second
constraint swap on a live ``user`` table. This revision does the remaining half.

``scim_token``
    One row per provisioning integration. Only the **SHA-256 digest** of the bearer
    token is stored — the token is displayed once, at creation — matching
    ``user_invitation`` and ``email_verification_token``. ``created_by`` is
    ``ON DELETE SET NULL``: provisioning must keep working after the administrator
    who issued the token leaves. ``revoked_at`` is set once and never cleared, and
    the row survives revocation so past audit events still resolve to a name.

``ck_group_mapping_source_valid`` → adds ``'proxy'``
    A ``group_mapping`` row can now key off the group names an authenticating
    reverse proxy asserts in its header, which is what lets trusted-header logins
    reuse ``services/idp_group_mapping_service`` instead of growing a second
    reconciler.

``ck_user_group_member_source_valid`` → adds ``'proxy'`` and ``'scim'``
    ``proxy`` marks a membership the header-driven reconciler owns.  ``scim`` marks
    one an identity provider wrote through ``/scim/v2/Groups``; it is **protected**
    alongside ``manual`` (``models/group.MEMBERSHIP_SOURCES_PROTECTED``), because a
    directory login pass must not delete what a provisioning system created — the
    two systems disagree about nothing except who is authoritative, and the answer
    is "whoever wrote the row".

Both constraint changes are widenings. No existing row can violate the new rule, so
there is no data migration and no risk to an in-place upgrade; the ``downgrade``
narrows them back and therefore first deletes the rows that would violate the old
rule, which is the honest mirror rather than a failing ALTER.

COMMUNITY EDITION: no behaviour change on upgrade. ``scim_token`` starts empty, and
with no token issued every ``/scim/v2/*`` request is a 401; ``proxy_enabled``
defaults false.

Revision ID: v382_scim_tokens
Revises: v381_approval_state
Create Date: 2026-08-07
"""

from alembic import op

revision = "v382_scim_tokens"
down_revision = "v381_approval_state"
branch_labels = None
depends_on = None

#: Kept as module-level constants so the CHECK bodies, the downgrade and the
#: consistency test cannot drift from ``app/models/group.py``, which builds the
#: identical strings from its own tuples. The test asserts they match.
MAPPING_SOURCES_SQL = "'ldap', 'oidc', 'proxy'"
MEMBERSHIP_SOURCES_SQL = "'manual', 'scim', 'ldap', 'oidc', 'proxy'"

#: The pre-v382 value sets, for ``downgrade``.
OLD_MAPPING_SOURCES_SQL = "'ldap', 'oidc'"
OLD_MEMBERSHIP_SOURCES_SQL = "'manual', 'ldap', 'oidc'"

#: Idempotent throughout: the startup runner stamps untracked databases by schema
#: fingerprint, so a revision routinely re-runs against a schema that already carries
#: part of its changes.
CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS scim_token (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL,
        name VARCHAR(255) NOT NULL,
        token_hash VARCHAR(64) NOT NULL,
        created_by INTEGER,
        expires_at TIMESTAMP WITH TIME ZONE,
        last_used_at TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS ix_scim_token_uuid ON scim_token (uuid);
    CREATE UNIQUE INDEX IF NOT EXISTS ix_scim_token_token_hash ON scim_token (token_hash);

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_scim_token_created_by'
        ) THEN
            ALTER TABLE scim_token ADD CONSTRAINT fk_scim_token_created_by
                FOREIGN KEY (created_by) REFERENCES "user" (id) ON DELETE SET NULL;
        END IF;
    END $$;
"""

_SOURCE_CHECKS_TEMPLATE = """
    ALTER TABLE group_mapping DROP CONSTRAINT IF EXISTS ck_group_mapping_source_valid;
    ALTER TABLE group_mapping ADD CONSTRAINT ck_group_mapping_source_valid
        CHECK (source IN ({mapping_sources}));

    ALTER TABLE user_group_member DROP CONSTRAINT IF EXISTS ck_user_group_member_source_valid;
    ALTER TABLE user_group_member ADD CONSTRAINT ck_user_group_member_source_valid
        CHECK (source IN ({membership_sources}));
"""

WIDEN_SOURCE_CHECKS_SQL = _SOURCE_CHECKS_TEMPLATE.format(  # nosec B608
    mapping_sources=MAPPING_SOURCES_SQL,
    membership_sources=MEMBERSHIP_SOURCES_SQL,
)

#: Narrowing back cannot leave rows the old rule rejects, so they go first. This is
#: destructive by necessity and only in the downgrade direction: a ``proxy`` mapping
#: or a ``scim`` membership has no meaning in a schema that predates them.
NARROW_SOURCE_CHECKS_SQL = f"""
    DELETE FROM group_mapping WHERE source NOT IN ({OLD_MAPPING_SOURCES_SQL});
    DELETE FROM user_group_member WHERE source NOT IN ({OLD_MEMBERSHIP_SOURCES_SQL});
""" + _SOURCE_CHECKS_TEMPLATE.format(  # nosec B608
    mapping_sources=OLD_MAPPING_SOURCES_SQL,
    membership_sources=OLD_MEMBERSHIP_SOURCES_SQL,
)

DROP_TABLE_SQL = """
    DROP INDEX IF EXISTS ix_scim_token_token_hash;
    DROP INDEX IF EXISTS ix_scim_token_uuid;
    DROP TABLE IF EXISTS scim_token;
"""


def upgrade():
    op.execute(CREATE_TABLE_SQL)
    op.execute(WIDEN_SOURCE_CHECKS_SQL)


def downgrade():
    op.execute(NARROW_SOURCE_CHECKS_SQL)
    op.execute(DROP_TABLE_SQL)
