"""Rename the OIDC identity columns and the ``auth_type`` value.

The companion to ``v377``, which moved the *configuration*. This one moves the
*identity*, and it is not cosmetic:

``user.keycloak_id`` -> ``user.oidc_subject``
    The value is an OIDC ``sub`` claim, which is unique **per issuer**, not globally.
    The old name asserted a global account identifier and made the eventual
    ``(iss, sub)`` key look like a refactor rather than a correction. Renaming it
    makes the constraint visible: the UNIQUE index on this column is sound only while
    exactly one provider is configured.

``user.keycloak_refresh_token`` -> ``user.oidc_refresh_token``
    Trivial rider. Still the encrypted provider refresh token used for federated
    logout.

``refresh_token.oidc_id_token``  (new)
    The ID token the session was established with, encrypted at rest. RP-Initiated
    Logout 1.0 needs it as ``id_token_hint``, so it has to outlive the callback — and
    it carries the user's full identity claim set, so it lives on the session row and
    **never in a cookie**. Its lifetime is the session's: rotation, revocation and the
    concurrent-session cap already delete these rows.

``auth_type`` ``'keycloak'`` -> ``'oidc'``
    User-visible in the admin Users table. ``ck_user_auth_type_valid`` (v375) and
    ``ck_user_invitation_auth_type_valid`` are dropped and re-added around the data
    update — and so is a **third** constraint that turned out to exist, the legacy
    ``users_auth_type_check``. See the note on ``_AUTH_TYPE_SWAP_TEMPLATE``: missing it
    would not have failed the migration, it would have failed every OIDC login
    afterwards.

**This must be one transaction, and Alembic gives us that** (a revision runs inside
the migration transaction unless it opts out). A half-applied state is not merely
untidy: ``auth/utils.py:local_password_allowed`` keys off ``AUTH_TYPES_*``, so rows
left at ``'keycloak'`` with the constant renamed would lock every OIDC user out — and
worse, ``api/endpoints/auth/mfa_tokens.py`` treats an unrecognised ``auth_type``
specially, so the same rows would be **exempt from MFA**. That is precisely the hazard
``v375`` was written to close.

The new CHECK set is ``('local', 'ldap', 'oidc', 'pki', 'proxy')``. ``'proxy'`` has no
implementation yet — ``auth/constants.VALID_AUTH_TYPES`` deliberately does not list
it, so nothing offers or accepts it — but trusted-header authentication is the next
phase, and swapping a CHECK constraint on a live ``user`` table is worth doing once
rather than twice. ``tests/unit/test_v378_migration_consistency.py`` pins the
subset relationship so the application constant can only ever be narrower.

COMMUNITY EDITION: a deployment that never used OIDC has no ``'keycloak'`` rows and no
non-NULL subject values; the renames are pure DDL and the new column starts NULL on
every existing session.

Revision ID: v378_oidc_identity_columns
Revises: v377_rename_keycloak_config_to_oidc
Create Date: 2026-08-07
"""

from alembic import op

revision = "v378_oidc_identity_columns"
down_revision = "v377_rename_keycloak_config_to_oidc"
branch_labels = None
depends_on = None

#: The auth types the database will accept after this revision. A superset of
#: ``app.auth.constants.VALID_AUTH_TYPES`` by design — see the module docstring.
VALID_AUTH_TYPES_SQL = "'local', 'ldap', 'oidc', 'pki', 'proxy'"

#: Guarded renames. ``ALTER TABLE ... RENAME COLUMN`` has no ``IF EXISTS`` form, so
#: each is wrapped in the information_schema probe every revision in this tree uses
#: (the ``v371`` shape). The ``NOT EXISTS`` half of each condition is what makes a
#: re-run against an already-renamed database a no-op instead of an error.
RENAME_COLUMNS_SQL = """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'keycloak_id'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'oidc_subject'
        ) THEN
            ALTER TABLE "user" RENAME COLUMN keycloak_id TO oidc_subject;
        END IF;

        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'keycloak_refresh_token'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'oidc_refresh_token'
        ) THEN
            ALTER TABLE "user" RENAME COLUMN keycloak_refresh_token TO oidc_refresh_token;
        END IF;

        -- A database that somehow reached this revision without either column at
        -- all (a hand-built schema) still needs them.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'oidc_subject'
        ) THEN
            ALTER TABLE "user" ADD COLUMN oidc_subject VARCHAR(255);
            CREATE UNIQUE INDEX IF NOT EXISTS ix_user_oidc_subject
                ON "user" (oidc_subject);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'oidc_refresh_token'
        ) THEN
            ALTER TABLE "user" ADD COLUMN oidc_refresh_token TEXT;
        END IF;
    END $$;

    ALTER TABLE refresh_token ADD COLUMN IF NOT EXISTS oidc_id_token TEXT;
"""

#: The data half plus the CHECK swaps. Dropping the constraints before the UPDATE is
#: mandatory: 'oidc' is not in the v375 constraint's value list, so the UPDATE would
#: be rejected row by row.
#:
#: ``users_auth_type_check`` is the load-bearing surprise here. ``user.auth_type``
#: carries TWO check constraints that say the same thing: the v375
#: ``ck_user_auth_type_valid`` and a much older ``users_auth_type_check`` (created by
#: ``v200``, re-asserted by ``v367``/``v371``, and still in ``database/init_db.sql``).
#: Swapping only the v375 one leaves the legacy one refusing ``'oidc'``, which does
#: not fail here — it fails later, at every single OIDC login, with a CheckViolation
#: on JIT provisioning. It is dropped rather than re-added: v375 made
#: ``ck_user_auth_type_valid`` unconditional and closed, so the second constraint is a
#: duplicate implementation of a rule that already has an owner, and keeping both in
#: sync forever is exactly the trap that produced this note.
#:
#: The template is filled from one module-level constant of literal SQL identifiers
#: and takes no input from anywhere — writing the value list out four times instead
#: would let the CHECK and the UPDATE drift apart, which is the larger risk.
_AUTH_TYPE_SWAP_TEMPLATE = """
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid;
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS users_auth_type_check;
    ALTER TABLE user_invitation
        DROP CONSTRAINT IF EXISTS ck_user_invitation_auth_type_valid;

    UPDATE "user" SET auth_type = 'oidc' WHERE auth_type = 'keycloak';
    UPDATE user_invitation SET auth_type = 'oidc' WHERE auth_type = 'keycloak';

    -- Anything still outside the new set would make the constraint unaddable. v375
    -- established 'local' as the safe interpretation of an unrecognised value: it
    -- is the model default and the only one that does not silently exempt the
    -- account from MFA enrolment.
    UPDATE "user" SET auth_type = 'local'
     WHERE auth_type IS NULL OR auth_type NOT IN ({types});
    UPDATE user_invitation SET auth_type = 'local'
     WHERE auth_type IS NULL OR auth_type NOT IN ({types});

    ALTER TABLE "user" ADD CONSTRAINT ck_user_auth_type_valid
        CHECK (auth_type IN ({types}));
    ALTER TABLE user_invitation ADD CONSTRAINT ck_user_invitation_auth_type_valid
        CHECK (auth_type IN ({types}));
"""

AUTH_TYPE_SWAP_SQL = _AUTH_TYPE_SWAP_TEMPLATE.format(types=VALID_AUTH_TYPES_SQL)  # nosec B608

#: Every CHECK on ``user.auth_type`` that must be gone or replaced. Module-level so
#: the consistency test can assert there is exactly ONE left afterwards — the shape
#: of this bug (a second constraint nobody remembered) recurs if it is not pinned.
LEGACY_AUTH_TYPE_CONSTRAINTS = ("users_auth_type_check",)

#: Mirror image, back to the v375 value set.
DOWNGRADE_SQL = """
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid;
    ALTER TABLE user_invitation
        DROP CONSTRAINT IF EXISTS ck_user_invitation_auth_type_valid;

    UPDATE "user" SET auth_type = 'keycloak' WHERE auth_type = 'oidc';
    UPDATE user_invitation SET auth_type = 'keycloak' WHERE auth_type = 'oidc';
    UPDATE "user" SET auth_type = 'local'
     WHERE auth_type IS NULL OR auth_type NOT IN ('local', 'ldap', 'keycloak', 'pki');
    UPDATE user_invitation SET auth_type = 'local'
     WHERE auth_type IS NULL OR auth_type NOT IN ('local', 'ldap', 'keycloak', 'pki');

    ALTER TABLE "user" ADD CONSTRAINT ck_user_auth_type_valid
        CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'));
    ALTER TABLE "user" ADD CONSTRAINT users_auth_type_check
        CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'));
    ALTER TABLE user_invitation ADD CONSTRAINT ck_user_invitation_auth_type_valid
        CHECK (auth_type IN ('local', 'ldap', 'keycloak', 'pki'));

    ALTER TABLE refresh_token DROP COLUMN IF EXISTS oidc_id_token;

    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'oidc_subject'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'keycloak_id'
        ) THEN
            ALTER TABLE "user" RENAME COLUMN oidc_subject TO keycloak_id;
        END IF;

        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'oidc_refresh_token'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'user' AND column_name = 'keycloak_refresh_token'
        ) THEN
            ALTER TABLE "user" RENAME COLUMN oidc_refresh_token TO keycloak_refresh_token;
        END IF;
    END $$;
"""


def upgrade():
    op.execute(RENAME_COLUMNS_SQL)
    op.execute(AUTH_TYPE_SWAP_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
