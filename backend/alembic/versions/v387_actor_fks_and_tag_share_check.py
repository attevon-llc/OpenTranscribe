"""v387: deleting an admin must not 500, and tag_share.target_type gets its CHECK.

Two repairs, both of which exist because a rule was written in one place and not
enforced in the other.

Part 1 — the five "who did this" FKs
-----------------------------------
``user`` is referenced by 46 foreign keys. Most are owner-scoped
(``media_file.user_id``, ``collection.user_id``) and are swept either by a DB
``ON DELETE CASCADE`` or by the hand-maintained lists in
``api/endpoints/admin._delete_user_owned_records`` /
``services/gdpr_erasure_service._delete_owner_scoped_rows``.

Five are different in kind: they record **who performed an action on somebody
else's row**. Nothing about the subject of the action tells you the actor is
being deleted, so no owner-scoped sweep can ever find them, and every one of
them was ``ON DELETE NO ACTION``:

===================================== ============================================
FK                                    what it records
===================================== ============================================
``auth_config.created_by``            the admin who first configured a method
``auth_config.updated_by``            the admin who last changed it
``auth_config_audit.changed_by``      the admin an audit entry is attributed to
``media_file.quarantined_by``         the admin who took *another user's* file down
``summary_prompt.shared_by``          the admin who flipped sharing on someone
                                      else's prompt (``prompts.share_prompt``
                                      accepts owner **or** admin)
===================================== ============================================

The consequence was a hard failure with no diagnosis: ``DELETE
/api/admin/users/{uuid}`` wraps the whole cascade in ``except Exception`` and
answers ``500 "User deletion failed"``, so deleting the admin who had ever
touched auth configuration — i.e. the person who set up OIDC and then left, the
single most likely account to be deleted — was simply impossible, and the error
named nothing. The live dev database carries 98 ``auth_config_audit`` rows: every
one of them pins its author's account in place.

**ON DELETE SET NULL, not a sixth entry in each deletion list.** There are two
independent deletion paths today and nothing compares them (see
``tests/unit/test_user_deletion_fk_coverage.py``, added with this revision); a
third path, or a hand-run ``DELETE FROM "user"``, would need the same additions
again. The database can enforce it once. This is also already the house style for
exactly this shape — ``scim_token.created_by``, ``email_notification_config.created_by``,
``watch_source.created_by``, ``user_invitation.created_user_id`` and
``user.approved_by`` are all ``SET NULL`` so the artifact outlives the admin who
made it.

``auth_config_audit.changed_by`` was additionally ``NOT NULL``, so it loses that
too. The **audit row survives** — deleting the record of a change because its
author left is the opposite of an audit trail — and the read path was already
written for this: ``api/endpoints/auth_config.get_audit_log`` filters
``if audit.changed_by is not None`` and renders a miss as unknown, with a comment
saying the referenced account "may have been deleted since". It could not be,
until now. The cost is that attribution degrades to NULL rather than being kept
as text; carrying the actor's email on the row would preserve it, but that needs
a writer change in ``services/auth_config_service.py`` and is a separate change.

Part 2 — ``tag_share.target_type``
----------------------------------
``v386`` mirrored ``collection_share`` deliberately: same target shape, same
CASCADEs, same partial unique indexes. It dropped exactly one guard.
``collection_share`` has ``_collection_share_target_type_check``;
``tag_share.target_type`` had nothing but a comment in ``models/sharing.py``
(``String(20)  # "user" or "group"``), and an unenforced comment is not a
constraint. A third value there is not inert: it reads as a grant whose target
kind no authorization branch matches.

The ``UPDATE`` before the CHECK is a repair, not a cleanup: exactly one of
``target_user_id`` / ``target_group_id`` is set (``_tag_share_target_check``
guarantees it), so the correct ``target_type`` is *derivable* for any row that
somehow carries a wrong one. Adding the constraint without it could fail the
migration, and a failed migration is ``SystemExit(1)`` — the backend does not
start.

Part 3 — the duplicate ``role`` CHECK
------------------------------------
``user.role`` carried **two** CHECK constraints with byte-identical bodies:
``ck_user_role_valid`` (the ``ck_*`` name the rest of this schema uses, and the one
``models/user.py`` and ``app/db/CLAUDE.md`` describe) and ``users_role_check``
(``v200_schema_reconciliation``). This is the exact shape ``v380`` had to fix on
``auth_type``: the widening was applied to one constraint and the other went on
refusing the new value, which does not fail during the migration — it fails later,
at every login of the new kind, as a ``CheckViolation`` on JIT provisioning.
``role`` has three values today and a fourth would hit the same wall. One rule, one
owner: the legacy duplicate is dropped, exactly as ``v380`` dropped
``users_auth_type_check``.

Revision ID: v387_actor_fks_and_tag_share_check
Revises: v386_add_tag_share
"""

from alembic import op

revision = "v387_actor_fks_and_tag_share_check"
down_revision = "v386_add_tag_share"
branch_labels = None
depends_on = None

#: One PL/pgSQL loop rather than five near-identical ``DO`` blocks, so the set of
#: repaired FKs is a readable list instead of something to diff. The guard is
#: ``confdeltype = 'n'`` (Postgres's code for ``SET NULL``): a database that
#: already has the rule is skipped entirely, which is what makes the whole
#: revision re-runnable against a partially-migrated schema.
#:
#: ``DROP NOT NULL`` runs for all five. It is a no-op on the four that are already
#: nullable, and stating it unconditionally records *why* it is needed: an
#: ``ON DELETE SET NULL`` on a ``NOT NULL`` column is a constraint that can only
#: ever fail.
ACTOR_FK_SQL = """
    DO $$
    DECLARE
        spec record;
    BEGIN
        FOR spec IN
            SELECT * FROM (VALUES
                ('auth_config',       'created_by',     'auth_config_created_by_fkey'),
                ('auth_config',       'updated_by',     'auth_config_updated_by_fkey'),
                ('auth_config_audit', 'changed_by',     'auth_config_audit_changed_by_fkey'),
                ('media_file',        'quarantined_by', 'media_file_quarantined_by_fkey'),
                ('summary_prompt',    'shared_by',      'summary_prompt_shared_by_fkey')
            ) AS t(tbl, col, fk)
        LOOP
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = spec.fk AND confdeltype = 'n'
            ) THEN
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN %I DROP NOT NULL', spec.tbl, spec.col
                );
                EXECUTE format(
                    'ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', spec.tbl, spec.fk
                );
                EXECUTE format(
                    'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) '
                    'REFERENCES "user"(id) ON DELETE SET NULL',
                    spec.tbl, spec.fk, spec.col
                );
            END IF;
        END LOOP;
    END $$;
"""

#: Derive-then-constrain. ``_tag_share_target_check`` already guarantees exactly one
#: target is set, so the right value is computable for every row and no row has to be
#: deleted or left behind for the CHECK to be addable.
TAG_SHARE_TYPE_SQL = """
    UPDATE tag_share
    SET target_type = CASE WHEN target_user_id IS NOT NULL THEN 'user' ELSE 'group' END
    WHERE target_type NOT IN ('user', 'group');

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = '_tag_share_target_type_check'
        ) THEN
            ALTER TABLE tag_share
                ADD CONSTRAINT _tag_share_target_type_check
                CHECK (target_type IN ('user', 'group'));
        END IF;
    END $$;
"""

#: Legacy duplicates of ``ck_user_role_valid``, dropped so a future widening of the role
#: set cannot be applied to one constraint and silently refused by the other. Named as a
#: module constant so the consistency test asserts on the same list this executes —
#: the shape ``v380.LEGACY_AUTH_TYPE_CONSTRAINTS`` established.
LEGACY_ROLE_CONSTRAINTS = ("users_role_check",)

ROLE_CHECK_SQL = """
    ALTER TABLE "user" DROP CONSTRAINT IF EXISTS users_role_check;

    -- Belt and braces: if a database somehow has the legacy duplicate and NOT the
    -- canonical one, dropping the duplicate would leave `role` unconstrained.
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_user_role_valid') THEN
            ALTER TABLE "user" ADD CONSTRAINT ck_user_role_valid
                CHECK (role IN ('user', 'admin', 'super_admin'));
        END IF;
    END $$;
"""

#: Module-level so ``tests/unit/test_v387_migration_consistency.py`` replays the real
#: statements instead of asserting on this file's source text — the failure mode that
#: let v386 ship a non-re-runnable ``op.create_table``.
UPGRADE_SQL = ACTOR_FK_SQL + TAG_SHARE_TYPE_SQL + ROLE_CHECK_SQL

#: Mirror image, with one deliberate asymmetry: ``changed_by``'s ``NOT NULL`` comes back
#: only if no row has been NULLed yet. Restoring it otherwise would mean deleting audit
#: rows whose author is gone — silently destroying the compliance trail to satisfy a
#: downgrade. The column staying nullable is the lesser divergence, and it is stated here
#: rather than discovered.
DOWNGRADE_SQL = """
    ALTER TABLE tag_share DROP CONSTRAINT IF EXISTS _tag_share_target_type_check;

    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check') THEN
            ALTER TABLE "user" ADD CONSTRAINT users_role_check
                CHECK (role IN ('user', 'admin', 'super_admin'));
        END IF;
    END $$;

    DO $$
    DECLARE
        spec record;
    BEGIN
        FOR spec IN
            SELECT * FROM (VALUES
                ('auth_config',       'created_by',     'auth_config_created_by_fkey'),
                ('auth_config',       'updated_by',     'auth_config_updated_by_fkey'),
                ('auth_config_audit', 'changed_by',     'auth_config_audit_changed_by_fkey'),
                ('media_file',        'quarantined_by', 'media_file_quarantined_by_fkey'),
                ('summary_prompt',    'shared_by',      'summary_prompt_shared_by_fkey')
            ) AS t(tbl, col, fk)
        LOOP
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', spec.tbl, spec.fk);
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES "user"(id)',
                spec.tbl, spec.fk, spec.col
            );
        END LOOP;

        IF NOT EXISTS (SELECT 1 FROM auth_config_audit WHERE changed_by IS NULL) THEN
            ALTER TABLE auth_config_audit ALTER COLUMN changed_by SET NOT NULL;
        END IF;
    END $$;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
