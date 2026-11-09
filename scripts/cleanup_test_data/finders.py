"""Read-only finders that turn the shapes into deletion candidates."""

from __future__ import annotations

from typing import Any, NamedTuple

from sqlalchemy import text

from cleanup_test_data.shapes import (
    INJECTED_TEST_SEED_SHAPE,
    MEDIA_FILENAME_SPECS,
    NAME_PREFIX_SPECS,
    NAME_PREFIXES,
    _name_matches_shape,
)

# ---------------------------------------------------------------------------------------------
# Candidate discovery (Postgres — read-only)
# ---------------------------------------------------------------------------------------------


class Candidate(NamedTuple):
    resource: str  # "media_file" | "collection" | "tag" | "watch_source" | "speaker_profile"
    #                 | "chat_conversation"
    uuid: str
    label: str  # filename / name / title, for reporting
    owner_email: str | None


def find_media_by_filename(conn: Any, cutoff: int) -> list[Candidate]:
    found: list[Candidate] = []
    for spec in MEDIA_FILENAME_SPECS:
        rows = conn.execute(
            text(
                """
                SELECT m.uuid, m.filename, u.email
                FROM media_file m
                JOIN "user" u ON u.id = m.user_id
                WHERE m.filename LIKE :pattern ESCAPE '\\'
                  AND m.upload_time < to_timestamp(:cutoff)
                """
            ),
            {'pattern': spec.sql_prefix, 'cutoff': cutoff},
        ).fetchall()
        for row in rows:
            filename = row.filename or ''
            if spec.shape.match(filename):
                found.append(Candidate('media_file', str(row.uuid), filename, row.email))
    return found


def find_injected_test_media(conn: Any, cutoff: int) -> list[Candidate]:
    """Files ``app.scripts.corpus_injection`` wrote under a registered TEST seed.

    Deleted through ``DELETE /api/files/{uuid}`` like every other media candidate, which ends
    in ``purge_media_file`` -- the same destroyer ``python -m app.scripts.corpus_injection
    --purge`` uses -- so the OpenSearch chunks and speaker rows go with the file.
    """
    rows = conn.execute(
        text(
            """
            SELECT m.uuid, m.filename, u.email,
                   m.metadata_important->'rag_eval'->>'seed' AS seed
            FROM media_file m
            JOIN "user" u ON u.id = m.user_id
            WHERE m.metadata_important->'rag_eval'->>'injected' = 'true'
              AND m.upload_time < to_timestamp(:cutoff)
            """
        ),
        {'cutoff': cutoff},
    ).fetchall()
    return [
        Candidate('media_file', str(row.uuid), f'{row.filename} [seed {row.seed}]', row.email)
        for row in rows
        if row.seed and INJECTED_TEST_SEED_SHAPE.fullmatch(row.seed)
    ]


def find_media_by_tier_a_owner(
    conn: Any, cutoff: int, unambiguous_email_patterns: list[str]
) -> list[Candidate]:
    """Media owned by a user matching a Tier-A (unambiguous) email pattern.

    Closes the corpus-owner gap (Decision 3.2): a user with ``files > 0`` is never a
    deletion candidate in ``cleanup-test-users.py``'s own sweep, so the search-quality
    corpus owner's files would otherwise never be reachable at all. Sweeping THESE
    first (before the user sweep runs) is what lets that user become deletable later
    in the same pass.
    """
    if not unambiguous_email_patterns:
        return []
    where = ' OR '.join(f"u.email LIKE '{p}' ESCAPE '\\'" for p in unambiguous_email_patterns)
    rows = conn.execute(
        text(
            f"""
            SELECT m.uuid, m.filename, u.email
            FROM media_file m
            JOIN "user" u ON u.id = m.user_id
            WHERE ({where})
              AND m.upload_time < to_timestamp(:cutoff)
            """
        ),
        {'cutoff': cutoff},
    ).fetchall()
    return [Candidate('media_file', str(row.uuid), row.filename or '', row.email) for row in rows]


def find_named_resources(conn: Any, cutoff: int) -> dict[str, list[Candidate]]:
    results: dict[str, list[Candidate]] = {}
    for resource, (table, column) in NAME_PREFIX_SPECS.items():
        time_col = 'upload_time' if table == 'media_file' else 'created_at'
        rows = conn.execute(
            text(
                f"""
                SELECT r.uuid, r.{column} AS label, u.email
                FROM "{table}" r
                LEFT JOIN "user" u ON u.id = r.user_id
                WHERE r.{time_col} < to_timestamp(:cutoff)
                """
            ),
            {'cutoff': cutoff},
        ).fetchall()
        matches = [
            Candidate(resource, str(row.uuid), row.label or '', row.email)
            for row in rows
            if row.label and _name_matches_shape(row.label, NAME_PREFIXES[resource], resource)
        ]
        results[resource] = matches
    return results
