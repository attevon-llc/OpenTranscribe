"""Audit rows can no longer be rewritten or deleted by an ordinary UPDATE or DELETE.

Why
---
Several tables are the database's record of what happened: ``auth_config_audit``,
``support_access_use`` and ``_grant``, ``erasure_ledger``, the ``takedown_*`` tables and
``legal_acceptance`` (which already has its own guard from v451). Nothing stopped an ORM bug, a
stray script or a compromised application connection from changing or deleting those rows.

This revision adds layer 1 of two (``docs/design/append_only_audit_plan.md``): a row trigger
``audit_row_guard()`` on each protected table that

* refuses DELETE and TRUNCATE;
* refuses an UPDATE that changes any column outside the table's lifecycle columns, except that
  a nullable actor column may go to NULL (the ``ON DELETE SET NULL`` of a user or organization
  deletion, so GDPR erasure keeps working);
* lets the TABLE OWNER through when ``opentranscribe.audit_maintenance = 'on'``. Migrations
  connect with that setting; a non-owner application role gets nothing from setting it.

In a single-role deployment the trigger is an accident guard, not a security boundary: the owner
can drop it. The boundary is layer 2, the optional app role (``POSTGRES_APP_USER``) that
``app.db.roles`` grants only the privileges the registry allows.

It also adds ``audit_prune(table, keep_days)``: a SECURITY DEFINER function that is the only way
the app role deletes audit rows, restricted to two insert-only tables and a hard floor of 365
days. ``legal_acceptance`` keeps its own retention sweep and is not touched here.

Safe on a populated table: functions and triggers only, no row is rewritten. Idempotent.
Downgrade drops the triggers and functions (no data loss).

Revision ID: v453_audit_row_immutability
Revises: v452_add_erasure_receipt
Create Date: 2026-11-01
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v453_audit_row_immutability"
down_revision = "v452_add_erasure_receipt"
branch_labels = None
depends_on = None

#: Frozen copy of ``app.db.audit_protection`` at this revision: (table, mutable, nulls).
#: A migration must not import a module that will change; the consistency test asserts equality.
#: ``erasure_ledger`` is last because the detection arm keys on its trigger.
GUARDED = (
    ("auth_config_audit", "{}", "{changed_by}"),
    ("support_access_use", "{}", "{}"),
    (
        "support_access_grant",
        "{decided_by_user_id,decided_at,decision,starts_at,expires_at,revoked_at,"
        "revoked_by_user_id}",
        "{organization_id,subject_user_id,grantee_user_id,decided_by_user_id,revoked_by_user_id}",
    ),
    ("takedown_notice", "{updated_at}", "{handled_by_user_id}"),
    (
        "takedown_notice_file",
        "{status,counter_notice_received_at,outcome_at,outcome_note,legal_hold_placed}",
        "{file_id,organization_id}",
    ),
    (
        "takedown_notification",
        "{status,attempted_at,recipient_user_id}",
        "{recipient_user_id}",
    ),
    (
        "erasure_ledger",
        "{status,completed_at,last_attempt_at,attempts,deferred_reason,"
        "legal_holds_outstanding,error_count,counters,resurrections_detected,"
        "last_resurrection_at}",
        "{actor_user_id}",
    ),
)

#: Frozen: tables ``audit_prune`` may age out, with the column that dates a row.
PRUNABLE = (("auth_config_audit", "created_at"), ("support_access_use", "occurred_at"))
RETENTION_FLOOR_DAYS = 365

FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION audit_maintenance_allowed(rel oid) RETURNS boolean
LANGUAGE sql STABLE AS $fn$
  SELECT coalesce(current_setting('opentranscribe.audit_maintenance', true), '') = 'on'
     AND (pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = rel), 'MEMBER')
          OR coalesce((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false));
$fn$;

CREATE OR REPLACE FUNCTION audit_row_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
  mutable text[] := coalesce(TG_ARGV[0], '{}')::text[];
  nulls text[] := coalesce(TG_ARGV[1], '{}')::text[];
  strict_nulls text[];
  o jsonb;
  n jsonb;
  c text;
BEGIN
  IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
    IF audit_maintenance_allowed(TG_RELID) THEN
      IF TG_LEVEL = 'ROW' THEN RETURN OLD; END IF;
      RETURN NULL;
    END IF;
    RAISE EXCEPTION 'audit records in % are append-only: % refused (see audit-integrity docs)',
      TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege';
  END IF;

  IF audit_maintenance_allowed(TG_RELID) THEN
    RETURN NEW;
  END IF;
  o := to_jsonb(OLD);
  n := to_jsonb(NEW);
  IF (n - mutable - nulls) IS DISTINCT FROM (o - mutable - nulls) THEN
    RAISE EXCEPTION 'audit records in % are append-only: UPDATE of a protected column refused '
      '(see audit-integrity docs)', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- A column that is also a lifecycle column may be set freely; the rest may only be cleared.
  strict_nulls := ARRAY(SELECT x FROM unnest(nulls) AS x WHERE NOT (x = ANY (mutable)));
  FOREACH c IN ARRAY strict_nulls LOOP
    IF (n -> c) IS DISTINCT FROM (o -> c) AND (n -> c) <> 'null'::jsonb THEN
      RAISE EXCEPTION 'audit records in %: column % may only be cleared, not rewritten',
        TG_TABLE_NAME, c USING ERRCODE = 'insufficient_privilege';
    END IF;
  END LOOP;
  RETURN NEW;
END $fn$;
"""

PRUNE_SQL = f"""
CREATE OR REPLACE FUNCTION audit_prune(tbl regclass, keep_days integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE
  rel text;
  col text;
  effective integer := greatest(coalesce(keep_days, 0), {RETENTION_FLOOR_DAYS});
  previous text := coalesce(current_setting('opentranscribe.audit_maintenance', true), '');
  removed bigint;
BEGIN
  SELECT c.relname INTO rel FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
   WHERE c.oid = tbl AND s.nspname = 'public';
  col := CASE rel
    {" ".join(f"WHEN '{t}' THEN '{c}'" for t, c in PRUNABLE)}
    ELSE NULL END;
  IF col IS NULL THEN
    RAISE EXCEPTION 'audit_prune: % is not a prunable audit table', tbl
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  PERFORM set_config('opentranscribe.audit_maintenance', 'on', true);
  EXECUTE format('DELETE FROM public.%I WHERE %I < now() - make_interval(days => $1)', rel, col)
    USING effective;
  GET DIAGNOSTICS removed = ROW_COUNT;
  PERFORM set_config('opentranscribe.audit_maintenance', previous, true);
  RETURN removed;
END $fn$;

REVOKE ALL ON FUNCTION audit_prune(regclass, integer) FROM PUBLIC;
"""


def _trigger_sql() -> str:
    parts = []
    for table, mutable, nulls in GUARDED:
        parts.append(
            f"""
DROP TRIGGER IF EXISTS trg_audit_guard_truncate_{table} ON {table};
CREATE TRIGGER trg_audit_guard_truncate_{table} BEFORE TRUNCATE ON {table}
  FOR EACH STATEMENT EXECUTE FUNCTION audit_row_guard('{mutable}', '{nulls}');
DROP TRIGGER IF EXISTS trg_audit_guard_{table} ON {table};
CREATE TRIGGER trg_audit_guard_{table} BEFORE UPDATE OR DELETE ON {table}
  FOR EACH ROW EXECUTE FUNCTION audit_row_guard('{mutable}', '{nulls}');
"""
        )
    return "".join(parts)


#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = FUNCTIONS_SQL + PRUNE_SQL + _trigger_sql()

DOWNGRADE_SQL = (
    "".join(
        f"DROP TRIGGER IF EXISTS trg_audit_guard_{t} ON {t};"
        f"DROP TRIGGER IF EXISTS trg_audit_guard_truncate_{t} ON {t};"
        for t, _, _ in GUARDED
    )
    + "DROP FUNCTION IF EXISTS audit_prune(regclass, integer);"
    "DROP FUNCTION IF EXISTS audit_row_guard();"
    "DROP FUNCTION IF EXISTS audit_maintenance_allowed(oid);"
)


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
