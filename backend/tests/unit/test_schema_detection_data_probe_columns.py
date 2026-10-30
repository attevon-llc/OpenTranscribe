"""A data-only revision's probe must not read a column the database may not have yet.

``v439``'s ``DETECT_SQL`` reads ``speaker.suggestion_source``, a column ``v091`` added long
after the ``speaker`` table existed. An untracked database at v010-v090 has ``speaker``,
``speaker_profile`` and ``speaker_match`` but not that column, so the probe raised
``UndefinedColumn``, ``_detect_schema_version`` failed and ``run_migrations`` took the backend
down. The fake connection raises the same way (``MissingColumnError``), so these tests fail on
the unfixed probe. The real-Postgres counterpart is
``tests/integration/test_schema_detection_untracked_legacy_db.py``.
"""

from __future__ import annotations

import pytest

from tests.unit._schema_detection_fake import DATA_COLUMNS
from tests.unit._schema_detection_fake import FakeSchema
from tests.unit._schema_detection_fake import MissingColumnError
from tests.unit._schema_detection_fake import col
from tests.unit._schema_detection_fake import detect
from tests.unit._schema_detection_fake import state

_V439 = "v439_infer_legacy_voiceprint_suggestion_source"
_SPEAKER_TABLES = {"speaker", "speaker_profile", "speaker_match"}
_PROBE = "column_name = 'suggestion_source'"


def test_speaker_tables_without_the_v091_column_do_not_raise():
    """Tables present, column absent: a pre-v091 database is detected, not crashed on."""
    schema = state(_SPEAKER_TABLES)
    assert detect(schema) == "v010_baseline"


def test_the_column_is_checked_before_the_data_probe_reads_it():
    schema = state(_SPEAKER_TABLES)
    detect(schema)
    assert not any("s.suggestion_source IS NULL" in sql for sql in schema.queries)
    assert any(_PROBE in sql for sql in schema.queries)


def test_with_the_column_the_data_probe_still_runs_after_the_existence_check():
    schema = state(_SPEAKER_TABLES, {col("speaker", "suggestion_source")})
    detect(schema)
    positions = {
        name: next(i for i, sql in enumerate(schema.queries) if needle in sql)
        for name, needle in (("exists", _PROBE), ("data", "s.suggestion_source IS NULL"))
    }
    assert positions["exists"] < positions["data"]


def test_without_the_speaker_tables_the_column_is_not_even_asked_about():
    schema = state()
    detect(schema)
    assert not any(_PROBE in sql for sql in schema.queries)


def test_the_fake_still_raises_on_a_missing_column():
    """Guard the guard: the fake must refuse the unguarded read, or the tests above prove nothing."""
    schema = FakeSchema(tables={"user", *_SPEAKER_TABLES})
    with pytest.raises(MissingColumnError):
        schema.answer(_detect_sql_of(_V439))


def test_every_modelled_data_column_is_a_real_data_revision():
    from tests.unit._schema_detection_fake import DATA_REVISIONS

    assert set(DATA_COLUMNS) <= set(DATA_REVISIONS)


def _detect_sql_of(revision: str) -> str:
    from tests.unit._schema_detection_fake import _detect_sql

    return _detect_sql(revision)
