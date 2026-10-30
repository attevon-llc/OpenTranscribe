"""A fake database connection for ``_detect_schema_version()``: a schema as a set of markers.

The ladder only ever asks ``SELECT EXISTS(...)`` questions (plus the data-only revisions' own
``DETECT_SQL``), so a schema can be modelled as tables plus markers (columns, indexes,
constraints, a trigger, settings rows, data rows). Each query is classified by shape and
answered from the markers. A query the fake cannot classify raises :class:`UnknownProbeError`
rather than answering False, and a query against a table the schema lacks raises
:class:`MissingTableError`, as Postgres would.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from app.db.migrations import _detect_schema_version

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

Marker = tuple[str, ...]

#: The data-only revisions whose arm keys on the ABSENCE of rows; the fake recognises their
#: probe by the revision's own ``DETECT_SQL`` and needs these tables, as the real query does.
DATA_REVISIONS = {
    "v434_reset_unusable_pyannote_source": {
        "user_setting",
        "user_diarization_settings",
        "user_asr_settings",
    },
    "v436_lite_local_speaker_detection_to_automatic": {"user_setting", "user_asr_settings"},
    "v439_infer_legacy_voiceprint_suggestion_source": {
        "speaker",
        "speaker_profile",
        "speaker_match",
    },
}


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _detect_sql(revision: str) -> str:
    spec = importlib.util.spec_from_file_location(revision, _VERSIONS / f"{revision}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _norm(module.DETECT_SQL)


_DATA_SQL = {_detect_sql(revision): revision for revision in DATA_REVISIONS}

_LEGACY_UUID = (
    "information_schema.columns WHERE table_schema='public' AND column_name='uuid' "
    "AND data_type <> 'uuid')"
)
_LEGACY_OIDC_KEYS = "FROM auth_config WHERE config_key LIKE 'keycloak\\_%' UNION ALL"


class UnknownProbeError(AssertionError):
    """The ladder asked something this fake does not model."""


class MissingTableError(AssertionError):
    """The ladder queried a table the schema does not have (Postgres would raise)."""


@dataclass
class FakeSchema:
    """A schema as a set of markers; ``tables`` also drives information_schema.tables."""

    tables: set[str] = field(default_factory=lambda: {"user"})
    markers: set[Marker] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)

    def answer(self, sql: str) -> bool:
        if sql in _DATA_SQL:
            revision = _DATA_SQL[sql]
            self.require(DATA_REVISIONS[revision])
            return ("data", revision) in self.markers
        if _LEGACY_UUID in sql:
            return ("legacy_varchar_uuid",) in self.markers
        if _LEGACY_OIDC_KEYS in sql:
            self.require({"auth_config", "auth_config_audit"})
            return ("legacy_oidc_config_keys",) in self.markers
        for pattern, rule in _RULES:
            if match := pattern.search(sql):
                return rule(self, match)
        raise UnknownProbeError(f"unclassified probe: {sql}")

    def require(self, needed: set[str]) -> None:
        missing = needed - self.tables
        if missing:
            raise MissingTableError(f"query against missing tables {sorted(missing)}")


def _column(schema: FakeSchema, m: re.Match[str]) -> bool:
    table, column, data_type = m.groups()
    if data_type:
        return ("column_type", table, column, data_type) in schema.markers
    return ("column", table, column) in schema.markers


def _any_table(schema: FakeSchema, m: re.Match[str]) -> bool:
    names = {name.strip().strip("'") for name in m.group(1).split(",")}
    return bool(names & schema.tables)


def _marker(kind: str) -> Callable[[FakeSchema, re.Match[str]], bool]:
    def rule(schema: FakeSchema, m: re.Match[str]) -> bool:
        return (kind, *m.groups()) in schema.markers

    return rule


def _setting(schema: FakeSchema, m: re.Match[str]) -> bool:
    schema.require({"system_settings"})
    return ("setting", m.group(1)) in schema.markers


#: Probe shape -> how the fake answers it. Order matters only where shapes overlap (the
#: constraint variants are tried before the bare-name form).
_RULES: list[tuple[re.Pattern[str], Callable[[FakeSchema, re.Match[str]], bool]]] = [
    (
        re.compile(
            r"information_schema\.columns WHERE table_name ?= ?'(\w+)' AND column_name ?= ?'(\w+)'"
            r"(?: AND data_type = '([^']+)')?\)$"
        ),
        _column,
    ),
    (re.compile(r"information_schema\.tables WHERE table_name IN \(([^)]*)\)\)$"), _any_table),
    (
        re.compile(r"information_schema\.tables WHERE table_name ?= ?'(\w+)'\)$"),
        lambda schema, m: m.group(1) in schema.tables,
    ),
    (
        re.compile(r"pg_indexes WHERE (?:tablename = '\w+' AND )?indexname ?= ?'(\w+)'\)$"),
        _marker("index"),
    ),
    (
        re.compile(r"pg_constraint WHERE conname = '(\w+)' AND confdeltype = 'n'\)$"),
        _marker("constraint_set_null"),
    ),
    (
        re.compile(
            r"pg_constraint WHERE conname = '(\w+)' "
            r"AND pg_get_constraintdef\(oid\) LIKE '%(\w+)%'\)$"
        ),
        _marker("constraint_def"),
    ),
    (re.compile(r"pg_constraint WHERE conname ?= ?'(\w+)'\)$"), _marker("constraint")),
    (
        re.compile(r"pg_enum e .* t\.typname = '(\w+)' AND e\.enumlabel = '(\w+)'\)$"),
        _marker("enum_label"),
    ),
    (re.compile(r"FROM pg_type WHERE typname = '(\w+)'\)$"), _marker("pg_type")),
    (re.compile(r"pg_trigger WHERE tgname = '(\w+)' AND NOT tgisinternal\)$"), _marker("trigger")),
    (re.compile(r"FROM system_settings WHERE key = '([\w.]+)'\)$"), _setting),
]


class _Result:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar(self) -> bool:
        return self._value


class FakeConn:
    """Records every query in ``schema.queries`` and answers it from the schema."""

    def __init__(self, schema: FakeSchema) -> None:
        self.schema = schema

    def execute(self, clause, params=None):
        sql = _norm(str(clause))
        self.schema.queries.append(sql)
        return _Result(self.schema.answer(sql))


def detect(schema: FakeSchema) -> str | None:
    """Run the real ladder against *schema*."""
    return _detect_schema_version(FakeConn(schema), sorted(schema.tables))


def col(table: str, column: str) -> Marker:
    return ("column", table, column)


def state(tables: Iterable[str] = (), markers: Iterable[Marker] = ()) -> FakeSchema:
    """``user`` plus the given tables and markers."""
    return FakeSchema(tables={"user", *tables}, markers=set(markers))
