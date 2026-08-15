"""Derive the enforcement rules each side *actually* declares, and diff them.

Split out of ``test_orm_ddl_divergence.py`` so the derivation can be called with a
substituted rule set — which is what the guard-the-guard tests do. A detector whose
matcher can never match is indistinguishable from a clean tree, and the only way to prove
otherwise is to hand it a rule set you constructed and check the answer changes.

Two rules about the comparison, both learned from ``test_schema_drift.py``:

* **Uniqueness is compared semantically, never by name.** Raw-SQL migrations name indexes
  ``idx_<table>_<col>`` / ``<table>_<cols>_key`` while SQLAlchemy's implicit name for
  ``unique=True`` is ``ix_<table>_<col>``, and ``Base`` deliberately carries no
  ``naming_convention`` (adding one would rename every existing constraint). Comparing
  names produced ~370 diffs that were not bugs. The *rule* — which key expressions, under
  which predicate — is what the database enforces and what a reader needs.
* **CHECK constraints ARE compared by name**, because a CHECK has no column tuple to key
  on and its body is reprinted by Postgres in a normalised form no textual comparison
  survives. The name is also the actionable handle: declaring a ``CheckConstraint`` with
  the database's exact name is both the visibility fix and what keeps autogenerate quiet.

Out of scope, deliberately: non-unique indexes (they enforce nothing — an index is a
performance property, and this is where the ~295 cosmetic naming diffs live) and
constraint deferrability (``tests/unit/test_schema_constraint_rejections.py`` already
pins ``user_pki_cert_unique`` as the schema's only DEFERRABLE constraint, in both
directions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CheckConstraint
from sqlalchemy import MetaData
from sqlalchemy import UniqueConstraint
from sqlalchemy import text

#: One uniqueness rule: (table, key expressions in index order, normalised predicate).
#: ``None`` predicate means total; anything else is a partial index.
UniqueRule = tuple[str, tuple[str, ...], str | None]

#: ``(table, constraint name)``.
CheckKey = tuple[str, str]

#: Every unique index that is not a primary key, with its key expressions rendered one at
#: a time. ``pg_get_indexdef(oid, k, true)`` gives column ``k``'s expression on its own —
#: parsing the parenthesised list out of the full ``CREATE UNIQUE INDEX`` text instead
#: would have to split on commas that also appear inside ``COALESCE(user_id, 0)``.
_DB_UNIQUE_SQL = """
SELECT cl.relname AS table_name,
       i.relname   AS index_name,
       (SELECT array_agg(pg_get_indexdef(ix.indexrelid, k, true) ORDER BY k)
          FROM generate_series(1, ix.indnkeyatts) AS k) AS key_exprs,
       pg_get_expr(ix.indpred, ix.indrelid) AS predicate
  FROM pg_index ix
  JOIN pg_class cl ON cl.oid = ix.indrelid
  JOIN pg_class i  ON i.oid  = ix.indexrelid
  JOIN pg_namespace n ON n.oid = cl.relnamespace
 WHERE n.nspname = 'public'
   AND ix.indisunique
   AND NOT ix.indisprimary
"""

_DB_CHECK_SQL = """
SELECT cl.relname, con.conname, pg_get_constraintdef(con.oid)
  FROM pg_constraint con
  JOIN pg_class cl ON cl.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = cl.relnamespace
 WHERE n.nspname = 'public' AND con.contype = 'c'
"""

_DB_TABLES_SQL = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"

#: ``(source)::text`` / ``ARRAY[...]::text[]`` — Postgres reprints every literal and column
#: reference with its cast, and a hand-written declaration never carries one.
_CAST = re.compile(r"::[a-z][a-z ]*(?:\[\])?", re.IGNORECASE)

#: Single-quoted literals, i.e. the *value set* a ``CHECK ... IN (...)`` enforces.
_LITERAL = re.compile(r"'([^']*)'")


def normalise(expression: Any) -> str | None:
    """Collapse an expression to a form both sides render identically.

    ``md5(text)`` from ``pg_get_indexdef`` and ``text("md5(text)")`` from the model differ
    only in whitespace; ``(source)::text = 'ldap'::text`` and ``source = 'ldap'`` differ
    only in casts and parentheses. Parentheses are dropped rather than balanced because
    Postgres adds them at every level and the declaration adds none — keeping them would
    make a correct declaration read as a divergence, which is the failure mode that turns
    a detector into noise nobody acts on.
    """
    if expression is None:
        return None
    collapsed = _CAST.sub("", str(expression)).replace('"', "")
    collapsed = re.sub(r"\s+", "", collapsed)
    return collapsed.replace("(", "").replace(")", "").lower()


def literals(check_definition: str) -> frozenset[str]:
    """The quoted values a CHECK admits — the part of its body that carries meaning.

    For the enum-shaped CHECKs that dominate this schema (``role``, ``auth_type``,
    ``source``, ``approval_status``) this is the whole enforcement content, and it is
    comparable across Postgres's ``= ANY (ARRAY[...])`` rewrite of a declared ``IN (...)``.
    For a boolean-logic CHECK both sides are empty and the comparison degenerates to "both
    exist", which is what the name-keyed check above already covers.
    """
    return frozenset(_LITERAL.findall(check_definition))


def db_unique_rules(conn: Any) -> dict[UniqueRule, list[str]]:
    """Uniqueness the database enforces right now, mapped to the object names carrying it."""
    rules: dict[UniqueRule, list[str]] = {}
    for table, index, keys, predicate in conn.execute(text(_DB_UNIQUE_SQL)).all():
        key = (table, tuple(normalise(k) or "" for k in keys), normalise(predicate))
        rules.setdefault(key, []).append(index)
    return rules


def db_check_constraints(conn: Any) -> dict[CheckKey, str]:
    """Every CHECK in the public schema, by ``(table, name)``."""
    return {
        (table, name): definition
        for table, name, definition in conn.execute(text(_DB_CHECK_SQL)).all()
    }


def db_tables(conn: Any) -> frozenset[str]:
    return frozenset(row[0] for row in conn.execute(text(_DB_TABLES_SQL)).all())


def _object_label(name: Any) -> str:
    """``<anonymous>`` for a constraint/index SQLAlchemy never named."""
    return name if isinstance(name, str) else "<anonymous>"


def orm_unique_rules(metadata: MetaData) -> dict[UniqueRule, list[str]]:
    """Uniqueness the models declare.

    Reads **both** ``table.constraints`` and ``table.indexes``, because a column-level
    ``unique=True`` lands in one or the other depending on whether ``index=True`` is also
    set: ``unique=True`` alone becomes an anonymous ``UniqueConstraint``, while
    ``unique=True, index=True`` becomes a unique ``Index``. Reading only the constraints
    would miss every ``uuid`` column in the schema and report the whole thing as divergent.
    """
    rules: dict[UniqueRule, list[str]] = {}
    for name, table in metadata.tables.items():
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                key = (name, tuple(normalise(c.name) or "" for c in constraint.columns), None)
                # A column-level ``unique=True`` yields a constraint whose ``name`` is
                # SQLAlchemy's ``_NONE_NAME`` sentinel object rather than a string — hence
                # ``isinstance`` and not a truthiness test.
                rules.setdefault(key, []).append(_object_label(constraint.name))
        for index in table.indexes:
            if not index.unique:
                continue
            keys = tuple(
                normalise(getattr(expr, "name", None) or expr) or "" for expr in index.expressions
            )
            predicate = index.dialect_kwargs.get("postgresql_where")
            rules.setdefault((name, keys, normalise(predicate)), []).append(
                _object_label(index.name)
            )
    return rules


def orm_check_constraints(metadata: MetaData) -> dict[CheckKey, str]:
    """Model-declared CHECKs, by ``(table, name)``. Unnamed ones cannot be matched at all."""
    found: dict[CheckKey, str] = {}
    for name, table in metadata.tables.items():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                found[(name, str(constraint.name))] = str(constraint.sqltext)
    return found


@dataclass(frozen=True)
class Divergence:
    """One disagreement between the database and ``Base.metadata``."""

    category: str
    table: str
    label: str
    detail: str

    @property
    def key(self) -> str:
        """Allowlist key. The category is part of it so one entry cannot cover four."""
        return f"{self.table}::{self.label}::{self.category}"


def _label(names: list[str], keys: tuple[str, ...]) -> str:
    """Prefer the object's name; fall back to its key tuple for an anonymous constraint."""
    named = [n for n in sorted(names) if not n.startswith("<")]
    return named[0] if named else "(" + ",".join(keys) + ")"


def find_divergences(
    *,
    db_uniques: dict[UniqueRule, list[str]],
    orm_uniques: dict[UniqueRule, list[str]],
    db_checks: dict[CheckKey, str],
    orm_checks: dict[CheckKey, str],
    orm_table_names: frozenset[str],
) -> list[Divergence]:
    """Diff the two rule sets, scoped to tables the ORM knows about.

    Scoping matters: a table with no model at all is ``test_schema_drift.py``'s finding,
    and reporting its constraints here too would put one defect in two suites. The
    companion assertion that the excluded set is only ``alembic_version`` is what keeps
    the scoping from quietly swallowing a real finding.
    """
    out: list[Divergence] = []

    for rule, names in db_uniques.items():
        table, keys, predicate = rule
        if table not in orm_table_names or rule in orm_uniques:
            continue
        out.append(
            Divergence(
                "db-only-unique",
                table,
                _label(names, keys),
                f"UNIQUE {list(keys)} predicate={predicate!r} is enforced by the database "
                "and declared nowhere in app/models",
            )
        )

    for rule, names in orm_uniques.items():
        table, keys, predicate = rule
        if rule in db_uniques:
            continue
        out.append(
            Divergence(
                "orm-only-unique",
                table,
                _label(names, keys),
                f"the models declare UNIQUE {list(keys)} predicate={predicate!r}, which no "
                "database index enforces",
            )
        )

    for (table, name), definition in db_checks.items():
        if table not in orm_table_names or (table, name) in orm_checks:
            continue
        out.append(
            Divergence("db-only-check", table, name, f"CHECK enforced only in DDL: {definition}")
        )

    for (table, name), sqltext in orm_checks.items():
        if (table, name) not in db_checks:
            out.append(
                Divergence("orm-only-check", table, name, f"declared but not enforced: {sqltext}")
            )

    for key in set(db_checks) & set(orm_checks):
        db_values, orm_values = literals(db_checks[key]), literals(orm_checks[key])
        if db_values != orm_values:
            out.append(
                Divergence(
                    "check-values-disagree",
                    key[0],
                    key[1],
                    f"database admits {sorted(db_values)}, the model declares {sorted(orm_values)}",
                )
            )

    return sorted(out, key=lambda d: d.key)
