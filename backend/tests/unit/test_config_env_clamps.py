"""Env-sourced values must be clamped identically to defaults (settings-validation bug class).

``pydantic-settings`` bypasses clamping logic written as a plain expression on a field's
default. ``DB_POOL_SIZE: int = max(_int_env("DB_POOL_SIZE", 20), 1)`` only runs that ``max()``
once, at class BODY execution time, to compute the field's *default* value — but ``Settings``
is a ``BaseSettings`` subclass, so pydantic-settings separately re-reads the matching env var at
**every** ``Settings()`` construction and casts it straight to ``int``, skipping the class-body
expression (and its clamp) entirely whenever the var is actually set. A negative
``DB_POOL_SIZE`` env var reached ``settings.DB_POOL_SIZE`` unclamped until
``Settings._clamp_pool_and_batch_floors`` (a ``field_validator``, which — unlike a default
expression — runs on env-sourced values too) was added.

Every field here is a **floor-only** clamp (``max(v, N)``, no ceiling), so there is no "above
the bound" case distinct from "inside the bound" to cover; instead each test proves a value
comfortably above the floor passes through untouched (no spurious over-clamping), in addition
to a below-floor value being clamped, an at-floor value staying put, and the default being
correct with no env var set at all.

Run in a clean child process (via ``run_in_clean_process`` from ``unit/conftest.py``), the same
pattern ``test_index_topology.py`` uses for its sibling clamp
(``OPENSEARCH_CHUNKS_INDEX_SHARDS``/``_REPLICAS``): the ``settings`` singleton binds once at
import time in this test process, so a real regression could only be observed by importing
``Settings`` fresh under a controlled environment.
"""

from __future__ import annotations

from app.core.config import DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS

_PRINT_FIELD_TEMPLATE = "from app.core.config import Settings; print(Settings().{field})"


def _read_field(run_in_clean_process, field: str, tmp_path, **env: str) -> str:
    # Annotated rather than returned directly: ``run_in_clean_process`` is an untyped
    # fixture, so mypy infers ``Any`` and flags ``no-any-return``. Naming the type here
    # keeps every downstream comparison checked, which a ``# type: ignore`` would not.
    out: str = run_in_clean_process(
        _PRINT_FIELD_TEMPLATE.format(field=field),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
        **env,
    )
    return out


# --------------------------------------------------------------------------------------- #
# DB_POOL_SIZE -- floor 1, default 20
# --------------------------------------------------------------------------------------- #


def test_db_pool_size_below_floor_is_clamped(run_in_clean_process, tmp_path):
    out = _read_field(run_in_clean_process, "DB_POOL_SIZE", tmp_path, DB_POOL_SIZE="-5")
    assert out == "1"


def test_db_pool_size_at_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(run_in_clean_process, "DB_POOL_SIZE", tmp_path, DB_POOL_SIZE="1")
    assert out == "1"


def test_db_pool_size_above_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(run_in_clean_process, "DB_POOL_SIZE", tmp_path, DB_POOL_SIZE="5")
    assert out == "5"


def test_db_pool_size_default_is_unclamped_baseline(run_in_clean_process, tmp_path):
    out = run_in_clean_process(
        _PRINT_FIELD_TEMPLATE.format(field="DB_POOL_SIZE"),
        unset=("DB_POOL_SIZE",),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    assert out == "20"


# --------------------------------------------------------------------------------------- #
# DB_MAX_OVERFLOW -- floor 0, default 40
# --------------------------------------------------------------------------------------- #


def test_db_max_overflow_below_floor_is_clamped(run_in_clean_process, tmp_path):
    out = _read_field(run_in_clean_process, "DB_MAX_OVERFLOW", tmp_path, DB_MAX_OVERFLOW="-10")
    assert out == "0"


def test_db_max_overflow_at_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(run_in_clean_process, "DB_MAX_OVERFLOW", tmp_path, DB_MAX_OVERFLOW="0")
    assert out == "0"


def test_db_max_overflow_above_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(run_in_clean_process, "DB_MAX_OVERFLOW", tmp_path, DB_MAX_OVERFLOW="15")
    assert out == "15"


def test_db_max_overflow_default_is_unclamped_baseline(run_in_clean_process, tmp_path):
    out = run_in_clean_process(
        _PRINT_FIELD_TEMPLATE.format(field="DB_MAX_OVERFLOW"),
        unset=("DB_MAX_OVERFLOW",),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    assert out == "40"


# --------------------------------------------------------------------------------------- #
# SEARCH_BULK_BATCH_SIZE -- floor 1, default 100
# --------------------------------------------------------------------------------------- #


def test_search_bulk_batch_size_below_floor_is_clamped(run_in_clean_process, tmp_path):
    out = _read_field(
        run_in_clean_process, "SEARCH_BULK_BATCH_SIZE", tmp_path, SEARCH_BULK_BATCH_SIZE="-3"
    )
    assert out == "1"


def test_search_bulk_batch_size_at_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(
        run_in_clean_process, "SEARCH_BULK_BATCH_SIZE", tmp_path, SEARCH_BULK_BATCH_SIZE="1"
    )
    assert out == "1"


def test_search_bulk_batch_size_above_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(
        run_in_clean_process, "SEARCH_BULK_BATCH_SIZE", tmp_path, SEARCH_BULK_BATCH_SIZE="7"
    )
    assert out == "7"


def test_search_bulk_batch_size_default_is_unclamped_baseline(run_in_clean_process, tmp_path):
    out = run_in_clean_process(
        _PRINT_FIELD_TEMPLATE.format(field="SEARCH_BULK_BATCH_SIZE"),
        unset=("SEARCH_BULK_BATCH_SIZE",),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    assert out == "100"


# --------------------------------------------------------------------------------------- #
# DB_IDLE_IN_TRANSACTION_TIMEOUT_MS -- floor 0, default DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS
# --------------------------------------------------------------------------------------- #


def test_db_idle_in_transaction_timeout_below_floor_is_clamped(run_in_clean_process, tmp_path):
    out = _read_field(
        run_in_clean_process,
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        tmp_path,
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="-100",
    )
    assert out == "0"


def test_db_idle_in_transaction_timeout_at_floor_is_untouched(run_in_clean_process, tmp_path):
    """0 is a supported, meaningful value (disables the backstop) -- must stay 0, not be
    forced positive by an over-eager clamp."""
    out = _read_field(
        run_in_clean_process,
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        tmp_path,
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="0",
    )
    assert out == "0"


def test_db_idle_in_transaction_timeout_above_floor_is_untouched(run_in_clean_process, tmp_path):
    out = _read_field(
        run_in_clean_process,
        "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        tmp_path,
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="60000",
    )
    assert out == "60000"


def test_db_idle_in_transaction_timeout_default_is_unclamped_baseline(
    run_in_clean_process, tmp_path
):
    out = run_in_clean_process(
        _PRINT_FIELD_TEMPLATE.format(field="DB_IDLE_IN_TRANSACTION_TIMEOUT_MS"),
        unset=("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    assert out == str(DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS)
