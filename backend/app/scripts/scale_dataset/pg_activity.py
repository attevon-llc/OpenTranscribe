"""Comments, per-user tags and cached waveforms for the scale seed (what the load test reads).

The query review only needed the core and governance tables. The concurrent-user load test
(``scripts/perf/load/``) also opens comment threads, filters the gallery by the person's own
tags and draws the waveform, so this adds those rows with the same rules as ``pg_synthetic``:
one deterministic ``INSERT ... SELECT FROM generate_series`` (or one ``UPDATE``) per table,
values derived from the seed id and the series index, never ``random()`` or ``now()``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Numeric
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.engine import Connection
from sqlalchemy.sql.expression import ColumnClause

from app.models.media import Comment
from app.models.media import MediaFile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.scripts.scale_dataset.pg_synthetic import ClaimLedger
from app.scripts.scale_dataset.pg_synthetic import Shape
from app.scripts.scale_dataset.pg_synthetic import _claim_ids
from app.scripts.scale_dataset.pg_synthetic import _file_user_id
from app.scripts.scale_dataset.pg_synthetic import _fill
from app.scripts.scale_dataset.pg_synthetic import _h
from app.scripts.scale_dataset.pg_synthetic import _table
from app.scripts.scale_dataset.pg_synthetic import _uuid

_ANCHOR = "timestamptz '2026-01-01 00:00:00+00'"
#: The ``samples`` the load test asks the waveform route for; the cache key is ``waveform_<n>``.
WAVEFORM_SAMPLES = 500


def _lit(sql: str) -> ColumnClause[Any]:
    return literal_column(sql)


def seed_activity(
    conn: Connection,
    shape: Shape,
    seed: str,
    base_user: int,
    base_file: int,
    ledger: ClaimLedger | None = None,
) -> dict[str, int]:
    """Insert comments and per-user tags, and cache a waveform on a share of the files.

    Args:
        conn: Connection inside the caller's transaction.
        shape: Row counts; ``comments``, ``user_tags`` and ``waveform_pm`` are used here.
        seed: Seed id (already validated by ``seed_dataset``).
        base_user: Largest ``user.id`` before this seed ran.
        base_file: Largest ``media_file.id`` before this seed ran.
        ledger: Collects the id ranges claimed here when given.
    """
    counts: dict[str, int] = {}
    g = "g"

    def owner(f: str) -> str:
        return _file_user_id(shape, seed, base_user, f)

    # A comment sits on file ((g - 1) % files) + 1, written by that file's owner, so every
    # person who opens their own file sees a thread of a few comments.
    cf = f"((g - 1) % {shape.files} + 1)"
    base_comment = _claim_ids(conn, _table(Comment), shape.comments, ledger)
    counts["comment"] = _fill(
        conn,
        _table(Comment),
        shape.comments,
        {
            "id": _lit(f"{base_comment} + g"),
            "uuid": _uuid(seed, "comment", g),
            "media_file_id": _lit(f"{base_file} + {cf}"),
            "user_id": _lit(owner(cf)),
            "text": _lit("'loadtest comment ' || g::text"),
            "timestamp": _lit(f"({_h(seed, 'cts', g)} % 3600)::float"),
            "created_at": _lit(f"{_ANCHOR} - ((g % 365) || ' days')::interval"),
        },
    )

    # Personal tags: tag g belongs to the owner of non-heavy file h(g) and is on that file, so
    # an ordinary person's GET /api/tags lists tags of their own and the tag filter finds files.
    rest = shape.files - shape.heavy_user_files
    tf = f"({shape.heavy_user_files} + 1 + (g - 1) % {max(rest, 1)})"
    base_tag = _claim_ids(conn, _table(Tag), shape.user_tags, ledger)
    counts["user_tag"] = _fill(
        conn,
        _table(Tag),
        shape.user_tags,
        {
            "id": _lit(f"{base_tag} + g"),
            "uuid": _uuid(seed, "usertag", g),
            "name": _lit("'loadtest-mytag-' || g::text"),
            "normalized_name": _lit("'loadtest-mytag-' || g::text"),
            "user_id": _lit(owner(tf)),
            "source": _lit("'manual'"),
        },
    )
    base_ft = _claim_ids(conn, _table(FileTag), shape.user_tags, ledger)
    counts["user_file_tag"] = _fill(
        conn,
        _table(FileTag),
        shape.user_tags,
        {
            "id": _lit(f"{base_ft} + g"),
            "uuid": _uuid(seed, "userfiletag", g),
            "media_file_id": _lit(f"{base_file} + {tf}"),
            "tag_id": _lit(f"{base_tag} + g"),
            "source": _lit("'manual'"),
        },
    )

    counts["waveform"] = _cache_waveforms(conn, shape, base_file)
    return counts


def _cache_waveforms(conn: Connection, shape: Shape, base_file: int) -> int:
    """Store the waveform the app would have cached on view, on the first ``waveform_pm`` of files.

    The shape is what ``api/endpoints/files/waveform.py`` writes and reads back (it treats an
    entry without ``extracted_samples`` / ``expected_duration`` as stale). The peaks are one
    fixed curve, computed once, so the cost is one constant rather than a per-row computation.
    """
    if shape.waveform_pm == 0:
        return 0
    n = WAVEFORM_SAMPLES
    series = func.generate_series(1, n).table_valued("i").render_derived(name="w")
    i = series.c.i
    peak = func.round(cast(0.5 + 0.45 * func.sin(i / 7.0), Numeric), 3)
    curve = select(func.jsonb_agg(aggregate_order_by(peak, i))).select_from(series)
    t_file = _table(MediaFile)
    duration = t_file.c.duration
    entry = func.jsonb_build_object(
        "waveform",
        curve.scalar_subquery(),
        "duration",
        duration,
        "expected_duration",
        duration,
        "extracted_samples",
        n,
        "sample_rate",
        16000,
        "samples",
        n,
    )
    index = t_file.c.id - base_file
    stmt = (
        update(t_file)
        .values(waveform_data=func.jsonb_build_object(f"waveform_{n}", entry))
        .where(index >= 1, index <= shape.files)
        .where((index - 1) * 1000 / shape.files < shape.waveform_pm)
    )
    return int(conn.execute(stmt).rowcount)
