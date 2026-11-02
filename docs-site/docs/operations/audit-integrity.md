---
sidebar_position: 8
title: Audit record integrity
description: Stop the application from rewriting or deleting audit records in the database, and enforce it with a second database role
---

# Audit record integrity

Several database tables are the record of what happened: the authentication configuration
change log, the support-access use log and grants, the erasure ledger, the takedown notice
tables and the legal acceptance records. This page explains how the application is prevented
from rewriting or deleting them, what that does and does not cover, and how to turn on the
enforced mode.

## What is protected

| Table | The application may | Never pruned |
|---|---|---|
| `auth_config_audit` | insert; clear the actor when that user is deleted | no (see [Retention](#retention)) |
| `support_access_use` | insert | no (see [Retention](#retention)) |
| `support_access_grant` | insert; set the decision, validity window and revocation | yes |
| `erasure_ledger` | insert; update status, attempt and counter columns | yes |
| `takedown_notice`, `takedown_notice_file`, `takedown_notification` | insert; update outcome and delivery columns | yes |
| `legal_acceptance` | insert; pseudonymise on account deletion; delete through its own retention sweep | own sweep |

Deleting a user or an organization still works: the foreign keys that point at them are
`ON DELETE SET NULL`, the audit row stays, and only the pointer is cleared. The row that
records who acted therefore survives the actor.

Two tables are deliberately **not** protected, each with its reason in
`backend/app/db/audit_protection.py`: the organization legal-hold tables (they belong to the
organization and are removed with it, and placement and release are already in the audit
stream) and `usage_event` (a table an edition may manage itself).

## What is NOT covered

- **The OpenSearch audit stream** (`audit-logs-YYYY.MM`) holds the main audit trail
  (authentication, administration, content and privacy events). This feature does not protect
  it. Its protection is the OpenSearch security plugin, which is off by default.
- **The audit fallback file** (`AUDIT_LOG_FALLBACK_PATH`) and the **erasure journal** are files
  owned by the application user and can be truncated by it. The standard output stream of the
  `audit` logger, collected by your container log driver or a SIEM, is the copy outside the
  application's reach.
- **Someone with the database owner password** (or shell access to the database host) can do
  anything. See [the two postures](#the-two-postures).

## Two layers

1. **Immutability trigger (always on).** A trigger on each protected table refuses `DELETE`,
   `TRUNCATE` and any `UPDATE` of a column outside the table's lifecycle columns. The refusal
   is SQLSTATE `42501` (`insufficient_privilege`) with a message naming the table. In a
   single-role installation this is an **accident guard**: it stops ORM bugs and stray
   scripts, but the table owner can still drop it.
2. **Application role (opt in).** A second database role, `POSTGRES_APP_USER`, is what the
   backend and workers connect as. It is not a superuser, owns nothing, and on the protected
   tables holds `SELECT`, `INSERT` and `UPDATE` on lifecycle columns only: no `DELETE`,
   `TRUNCATE`, `TRIGGER` or `REFERENCES`. This is the boundary: even with the trigger dropped,
   the role cannot change the data.

`POSTGRES_USER` stays the owner and migration role.

## Enable it

Set both values in `.env` and recreate the containers:

```bash
POSTGRES_APP_USER=ot_app
POSTGRES_APP_PASSWORD=<a long random value>
```

```bash
./opentranscribe.sh restart
```

Leave `DATABASE_URL` blank (it is built from the application credentials). If you manage the
connection strings yourself, set `MIGRATION_DATABASE_URL` to the owner and point `DATABASE_URL`
at the application role; setting `POSTGRES_APP_USER` together with an explicit `DATABASE_URL`
and no `MIGRATION_DATABASE_URL` is refused at startup.

On every start the backend migrates as the owner, creates or updates the role, re-applies its
grants from one registry, and verifies by measurement that the runtime role really cannot
rewrite audit rows. **If you asked for the split and it is not effective** (for example the
role turned out to be a superuser, or owns a protected table), the backend and the workers
refuse to start and log the reason.

The `docker-compose.db-roles.yml` overlay is loaded automatically when `POSTGRES_APP_USER` is
set. It removes the owner password from every service except the backend, so a worker that
runs ffmpeg or model code on untrusted media never holds it.

To disable, unset both values and recreate. Everything connects as the owner again; the
now-unused role is left in place and is never dropped by the application.

### Managed databases

If the role is created by your infrastructure tooling, set `POSTGRES_APP_ROLE_PROVISION=false`.
The grants are still applied on every start. The owner needs the right to grant privileges on
the tables; to create the role itself it also needs `CREATEROLE`.

## The two postures

By default the backend runs the migrations, so it must also hold the owner credentials. The
role split protects against SQL injection, ORM bugs and every worker, but code execution
inside the backend process can still reach the owner password.

For full separation set `RUN_MIGRATIONS_ON_STARTUP=false`, give the backend only the
application credentials, and run the migrations in a separate job that holds the owner
credentials:

```bash
alembic upgrade head
python -m app.db.roles reconcile
```

The second command re-applies the grants and is required after each upgrade and after a
database restore.

## Verify

**Settings > Audit Logs** shows an *Audit record protection* row: **Enforced**, or **Not
enforced** with the note that the guard is an accident guard only. It is read from the
database (`has_table_privilege`), not from the configuration. The same measurement is
available to a super administrator at `GET /api/admin/security/audit-integrity`.

## Errors you may see

| Message | Meaning |
|---|---|
| `audit records in <table> are append-only: DELETE refused` | Something tried to delete an audit row. Delete audit data only through the documented retention sweep. |
| `audit records in <table> are append-only: UPDATE of a protected column refused` | Something tried to change a recorded value. Lifecycle columns (status, outcome, revocation) remain updatable. |
| `database role split was requested ... but is not effective` | The application role can still rewrite audit rows. The message lists why. |

A data migration that must repair an audit row runs under a maintenance setting that the
migration runner applies and that only the table owner can use.

## Restores

A backup is taken with `--no-acl`, so a restored database has no grants for the application
role. The backend re-applies them on its next start (or run `python -m app.db.roles reconcile`
in a migrate-job deployment). The immutability triggers are part of the dump and come back
with it.

## Retention

`AUDIT_LOG_RETENTION_DAYS` controls how long rows are kept in the prunable Postgres audit
tables (`auth_config_audit` and `support_access_use`):

- `0` (the default) keeps them forever.
- Any other value must be **365 or more**; a smaller value is refused at startup.
- A daily sweep deletes rows older than the value through a database function that refuses
  every other table and enforces the 365-day floor itself, so a wrong value can never delete a
  younger row.

The erasure ledger, takedown records and support-access grants are evidence and are never
pruned. Legal acceptance records have their own retention (see
[Legal documents](../legal/legal-documents.md)). OpenSearch index retention is separate and is
not managed here.

## Upgrade and rollback

- **Upgrading** adds the triggers and changes nothing else; the role split stays off until you
  enable it. If a code path legitimately changes a protected row in a way the application does
  not expect, it now fails loudly with the error above instead of silently succeeding.
- **Rolling back** to a release without this feature: restore the pre-upgrade backup, or run
  `alembic downgrade` by one revision (it drops the triggers and functions, no data is lost).
  Unset `POSTGRES_APP_USER` first; older releases ignore the setting and connect as the owner.
