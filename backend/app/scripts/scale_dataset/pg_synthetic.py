"""Set-based, deterministic synthetic rows for the query review (issue #1337).

Every table is filled by one ``INSERT ... SELECT ... FROM generate_series`` built with SQLAlchemy
Core ``insert(table).from_select`` so the column names come from the models (a renamed or removed
column raises here instead of seeding the wrong shape). Values derive from the series index and
the seed id through ``hashtext``, never ``random()`` or ``now()``, so two seeds are identical.
PostgreSQL "Populating a Database": https://www.postgresql.org/docs/17/populate.html.

Rows are inert in the injector's sense (``corpus_injection/injector.py``): terminal statuses and
``completed_at`` NULL, so no post-transcription sweep or worker picks them up. No OpenSearch
document and no MinIO object is created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import literal_column
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import REGCLASS
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.expression import ColumnClause
from sqlalchemy.sql.expression import FromClause

from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.user import User

SEED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
EMAIL_DOMAIN = "loadtest.invalid"
_ANCHOR = "timestamptz '2026-01-01 00:00:00+00'"  # fixed, so reruns are byte-identical


@dataclass(frozen=True)
class Shape:
    """Row counts and ratios. Ratios are per mille (parts in 1000)."""

    users: int = 1000
    files: int = 100_000
    heavy_user_files: int = 20_000
    speakers_per_file: int = 10
    segments_per_file: int = 100
    tasks_per_file: int = 3
    profiles: int = 20_000
    tags: int = 2_000
    tags_per_file: int = 3
    trashed_pm: int = 20
    quarantined_pm: int = 5
    legal_hold_pm: int = 2
    voiceprint_pm: int = 50
    voiceprint_link_pm: int = 20
    display_name_pm: int = 300
    suggestion_pm: int = 30
    profile_linked_pm: int = 400
    orgs: int = 5
    holds: int = 50
    hold_files: int = 2_000
    released_hold_pm: int = 200
    platform_hold_pm: int = 300
    collections: int = 5_000
    collection_members: int = 200_000
    collection_shares: int = 2_000
    org_collection_pm: int = 600
    clusters: int = 50_000
    cluster_members: int = 500_000
    matches: int = 500_000
    hidden: int = 5_000
    conversations: int = 20_000
    messages: int = 200_000
    comments: int = 300_000
    user_tags: int = 20_000
    waveform_pm: int = 0

    def max_rows(self) -> int:
        """An upper bound on the rows any one table receives, for reserving its id range."""
        spf, files = self.speakers_per_file, self.files
        return max(
            self.users,
            files * spf,
            files * self.segments_per_file,
            files * self.tags_per_file,
            self.profiles,
            self.tags,
            self.orgs,
            self.holds,
            self.hold_files,
            self.collections,
            self.collection_members,
            self.collection_shares,
            self.clusters,
            self.cluster_members,
            self.matches,
            self.hidden,
            self.conversations,
            self.messages,
        )

    def __post_init__(self) -> None:
        if self.users < 3:
            raise ValueError("users must be >= 3 (admin, heavy user, one ordinary user)")
        if self.tags < 3:
            raise ValueError("tags must be >= 3")
        if self.heavy_user_files > self.files:
            raise ValueError("heavy_user_files cannot exceed files")
        spk = self.files * self.speakers_per_file
        limits = {
            "hold_files": self.holds * self.files,
            "collection_members": self.collections * self.files,
            "cluster_members": self.clusters * spk,
            "matches": spk - 1,
            "hidden": self.files,
        }
        for name, cap in limits.items():
            if getattr(self, name) > cap:
                raise ValueError(f"{name} cannot exceed {cap} for this shape")
        if min(self.orgs, self.holds, self.collections, self.clusters, self.conversations) < 1:
            raise ValueError("orgs, holds, collections, clusters, conversations must be >= 1")
        if min(self.files, self.speakers_per_file, self.segments_per_file, self.profiles) < 1:
            raise ValueError("files, speakers_per_file, segments_per_file, profiles must be >= 1")
        if self.comments < 0:
            raise ValueError("comments must be >= 0")
        if not 0 <= self.user_tags <= self.files - self.heavy_user_files:
            raise ValueError("user_tags must be between 0 and files - heavy_user_files")
        if not 0 <= self.waveform_pm <= 1000:
            raise ValueError("waveform_pm must be between 0 and 1000")


def _check_seed(seed: str) -> str:
    if not SEED_ID_RE.match(seed):
        raise ValueError(f"seed id must match {SEED_ID_RE.pattern}, got {seed!r}")
    return seed


def _h(seed: str, salt: str, g: object) -> str:
    """Deterministic non-negative int from (seed, salt, index)."""
    return f"(hashtext('{seed}:{salt}:' || {g}::text)::bigint & 2147483647)"


def _uuid(seed: str, salt: str, g: object) -> ColumnClause[Any]:
    return literal_column(f"md5('{seed}:{salt}:' || {g}::text)::uuid")


def _table(model: type[DeclarativeBase]) -> Table:
    table = model.__table__
    if not isinstance(table, Table):
        raise TypeError(f"{model.__name__} is not mapped to a plain table")
    return table


def _series(n: int) -> FromClause:
    return func.generate_series(1, n).table_valued("g").render_derived(name="s")


def _fill(conn: Connection, table: Table, n: int, cols: dict[str, ColumnClause[Any]]) -> int:
    g = _series(n)
    stmt = insert(table).from_select(list(cols), select(*cols.values()).select_from(g))
    conn.execute(stmt)
    return n


def _claim_ids(conn: Connection, table: Table, rows: int) -> int:
    """Reserve ``rows`` ids from the table's sequence and return the base (new id = base + g).

    Taking the base from ``max(id)`` alone is only safe on a database nobody else is writing
    to. On a shared one, two seeds read the same maximum and insert the same explicit ids, and
    an ordinary insert drawing from the sequence lands inside the seeded range; each waits on
    the other's uncommitted row and Postgres reports a deadlock on the primary key. One
    ``setval`` statement moves the sequence past the whole range before any row is written, so
    every other writer's ``nextval`` is already beyond it.
    """
    seq = func.pg_get_serial_sequence(f'"{table.name}"', "id")
    last = func.coalesce(func.pg_sequence_last_value(cast(seq, REGCLASS)), 0)
    top = select(func.coalesce(func.max(table.c.id), 0)).scalar_subquery()
    claimed = conn.execute(select(func.setval(seq, func.greatest(top, last) + rows))).scalar_one()
    return int(claimed) - rows


def _bump_sequence(conn: Connection, table: Table) -> None:
    """Leave the sequence at or past every seeded id (never behind a value already handed out)."""
    seq = func.pg_get_serial_sequence(f'"{table.name}"', "id")
    last = func.coalesce(func.pg_sequence_last_value(cast(seq, REGCLASS)), 0)
    top = select(func.greatest(func.max(table.c.id), last, 1)).scalar_subquery()
    conn.execute(select(func.setval(seq, top)))


def _file_user_id(shape: Shape, seed: str, base_user: int, f: str) -> str:
    """User id owning file index ``f``: the first ``heavy_user_files`` belong to user 2."""
    rest = max(shape.users - 2, 1)
    return (
        f"(CASE WHEN {f} <= {shape.heavy_user_files} THEN {base_user + 2} "
        f"ELSE {base_user + 3} + (hashtext('{seed}:owner:' || {f}::text)::bigint "
        f"& 2147483647) % {rest} END)"
    )


def claim_core_bases(conn: Connection, shape: Shape) -> tuple[int, int, int]:
    """Claimed id bases for users, media files and speakers (see :func:`_claim_ids`)."""
    n = shape.max_rows()
    return (
        _claim_ids(conn, _table(User), n),
        _claim_ids(conn, _table(MediaFile), n),
        _claim_ids(conn, _table(Speaker), n),
    )


def seed_dataset(
    conn: Connection,
    shape: Shape,
    seed: str,
    password_hash: str,
    bases: tuple[int, int, int] | None = None,
) -> dict[str, int]:
    """Insert the whole dataset on ``conn`` and return rows written per table.

    The caller owns the transaction and ``ANALYZE`` (a tiny test seed rolls back; the CLI commits
    and analyzes). User 1 is the admin, user 2 the heavy user, the rest ordinary. ``bases`` is
    the already-claimed ``(user, file, speaker)`` id bases when the caller needs them first.
    """
    seed = _check_seed(seed)
    counts: dict[str, int] = {}
    t_user, t_file = _table(User), _table(MediaFile)
    t_spk, t_prof = _table(Speaker), _table(SpeakerProfile)

    n = shape.max_rows()
    if bases is None:
        bases = claim_core_bases(conn, shape)
    base_user, base_file, base_spk = bases
    base_prof = _claim_ids(conn, t_prof, n)
    base_tag = _claim_ids(conn, _table(Tag), n)
    base_seg = _claim_ids(conn, _table(TranscriptSegment), n)
    base_ft = _claim_ids(conn, _table(FileTag), n)

    g: ColumnClause[Any] = literal_column("g")
    counts["user"] = _fill(
        conn,
        t_user,
        shape.users,
        {
            "id": literal_column(f"{base_user} + g"),
            "uuid": _uuid(seed, "user", g),
            "email": literal_column(f"'perf-' || g::text || '@{EMAIL_DOMAIN}'"),
            "full_name": literal_column("'Perf User ' || g::text"),
            "hashed_password": literal_column("'" + password_hash.replace("'", "''") + "'"),
            "is_active": literal_column("true"),
            "is_superuser": literal_column("false"),
            "role": literal_column("CASE WHEN g = 1 THEN 'admin' ELSE 'user' END"),
            "platform_super_admin_link_authorized": literal_column("false"),
            "auth_type": literal_column("'local'"),
            "allow_local_fallback": literal_column("false"),
            "must_change_password": literal_column("false"),
            "email_verified": literal_column("true"),
            "created_at": literal_column(f"{_ANCHOR} - (g || ' minutes')::interval"),
            "updated_at": literal_column(f"{_ANCHOR}"),
        },
    )

    def owner(f: str) -> str:
        return _file_user_id(shape, seed, base_user, f)

    def h(salt: str) -> str:
        return _h(seed, salt, "g")

    counts["media_file"] = _fill(
        conn,
        t_file,
        shape.files,
        {
            "id": literal_column(f"{base_file} + g"),
            "uuid": _uuid(seed, "file", g),
            "user_id": literal_column(owner("g")),
            "filename": literal_column("'loadtest-' || g::text || '.mp3'"),
            "title": literal_column("'Loadtest recording ' || g::text"),
            "storage_path": literal_column(f"'loadtest/{seed}/' || g::text"),
            "file_size": literal_column(f"1000000 + {h('size')} % 500000000"),
            "content_type": literal_column(
                f"CASE WHEN {h('ct')} % 4 = 0 THEN 'video/mp4' ELSE 'audio/mpeg' END"
            ),
            "status": literal_column("'completed'"),
            "duration": literal_column(f"60 + {h('dur')} % 7200"),
            "upload_time": literal_column(
                f"{_ANCHOR} - (({h('up')} % 365) || ' days')::interval "
                f"- (({h('upm')} % 1440) || ' minutes')::interval"
            ),
            "metadata_important": literal_column(
                f"jsonb_build_object('loadtest', '{seed}', 'format', "
                f"CASE WHEN {h('fmt')} % 3 = 0 THEN 'mp4' ELSE 'mp3' END, "
                f"'codec', CASE WHEN {h('cod')} % 2 = 0 THEN 'aac' ELSE 'mp3' END)"
            ),
            "is_quarantined": literal_column(f"(g % 1000) < {shape.quarantined_pm}"),
            "legal_hold": literal_column(
                f"(g % 1000) >= {shape.quarantined_pm} "
                f"AND (g % 1000) < {shape.quarantined_pm + shape.legal_hold_pm}"
            ),
            "deleted_at": literal_column(
                f"CASE WHEN (g % 1000) >= 500 AND (g % 1000) < {500 + shape.trashed_pm} "
                f"THEN {_ANCHOR} - ((g % 30) || ' days')::interval END"
            ),
            "purge_after": literal_column(
                f"CASE WHEN (g % 1000) >= 500 AND (g % 1000) < {500 + shape.trashed_pm} "
                f"THEN {_ANCHOR} + ((g % 30) || ' days')::interval END"
            ),
            "trash_origin": literal_column(
                f"CASE WHEN (g % 1000) >= 500 AND (g % 1000) < {500 + shape.trashed_pm} "
                f"THEN 'user' END"
            ),
        },
    )

    counts["speaker_profile"] = _fill(
        conn,
        t_prof,
        shape.profiles,
        {
            "id": literal_column(f"{base_prof} + g"),
            "uuid": _uuid(seed, "profile", g),
            "user_id": literal_column(f"{base_user} + 2 + {h('pu')} % {shape.users - 1}"),
            "name": literal_column("'Perf Person ' || g::text"),
        },
    )

    spf = shape.speakers_per_file
    f_of = f"((g - 1) / {spf} + 1)"
    counts["speaker"] = _fill(
        conn,
        t_spk,
        shape.files * spf,
        {
            "id": literal_column(f"{base_spk} + g"),
            "uuid": _uuid(seed, "speaker", g),
            "user_id": literal_column(owner(f_of)),
            "media_file_id": literal_column(f"{base_file} + {f_of}"),
            "name": literal_column(f"'SPEAKER_' || lpad(((g - 1) % {spf})::text, 2, '0')"),
            "display_name": literal_column(
                f"CASE WHEN {h('dn')} % 1000 < {shape.display_name_pm} "
                f"THEN 'Perf Person ' || ({h('dn2')} % {shape.profiles} + 1)::text END"
            ),
            "suggestion_source": literal_column(
                f"CASE WHEN {h('ss')} % 1000 < {shape.suggestion_pm} THEN 'llm' END"
            ),
            "identity_source": literal_column(
                f"CASE WHEN {h('is')} % 1000 < {shape.voiceprint_pm} THEN 'voiceprint' "
                f"WHEN {h('is')} % 1000 < {shape.voiceprint_pm + shape.voiceprint_link_pm} "
                f"THEN 'voiceprint_link' END"
            ),
            "profile_id": literal_column(
                f"CASE WHEN {h('pl')} % 1000 < {shape.profile_linked_pm} "
                f"THEN {base_prof} + 1 + {h('pl2')} % {shape.profiles} END"
            ),
            "verified": literal_column(f"{h('vf')} % 1000 >= 600"),
        },
    )

    spg = shape.segments_per_file
    sf = f"((g - 1) / {spg} + 1)"
    counts["transcript_segment"] = _fill(
        conn,
        _table(TranscriptSegment),
        shape.files * spg,
        {
            "id": literal_column(f"{base_seg} + g"),
            "uuid": _uuid(seed, "segment", g),
            "media_file_id": literal_column(f"{base_file} + {sf}"),
            "speaker_id": literal_column(
                f"{base_spk} + ({sf} - 1) * {spf} + 1 + ((g - 1) % {spg}) % {spf}"
            ),
            "start_time": literal_column(f"((g - 1) % {spg}) * 5.0"),
            "end_time": literal_column(f"((g - 1) % {spg}) * 5.0 + 4.5"),
            "text": literal_column("'loadtest segment ' || g::text || ' lorem ipsum dolor'"),
            "is_overlap": literal_column("false"),
        },
    )

    tpf = shape.tasks_per_file
    tf = f"((g - 1) / {tpf} + 1)"
    counts["task"] = _fill(
        conn,
        _table(Task),
        shape.files * tpf,
        {
            "id": literal_column(f"'loadtest-{seed}-' || g::text"),
            "user_id": literal_column(owner(tf)),
            "media_file_id": literal_column(f"{base_file} + {tf}"),
            "task_type": literal_column(
                f"(ARRAY['transcription','summarization','analytics'])[1 + ((g - 1) % {tpf}) % 3]"
            ),
            "status": literal_column("'completed'"),
            "progress": literal_column("1.0"),
            "created_at": literal_column(f"{_ANCHOR} - ((g % 365) || ' days')::interval"),
        },
    )

    counts["tag"] = _fill(
        conn,
        _table(Tag),
        shape.tags,
        {
            "id": literal_column(f"{base_tag} + g"),
            "uuid": _uuid(seed, "tag", g),
            "name": literal_column("'loadtest-tag-' || g::text"),
            "normalized_name": literal_column("'loadtest-tag-' || g::text"),
            "user_id": literal_column(f"{base_user} + 2"),
            "source": literal_column("'manual'"),
        },
    )

    tg = shape.tags_per_file
    ff = f"((g - 1) / {tg} + 1)"
    step = max(shape.tags // tg, 1)
    counts["file_tag"] = _fill(
        conn,
        _table(FileTag),
        shape.files * tg,
        {
            "id": literal_column(f"{base_ft} + g"),
            "uuid": _uuid(seed, "filetag", g),
            "media_file_id": literal_column(f"{base_file} + {ff}"),
            "tag_id": literal_column(
                f"{base_tag} + 1 + ({ff} + ((g - 1) % {tg}) * {step}) % {shape.tags}"
            ),
            "source": literal_column("'manual'"),
        },
    )

    for table in (
        t_user,
        t_file,
        t_prof,
        t_spk,
        _table(Tag),
        _table(FileTag),
        _table(TranscriptSegment),
    ):
        _bump_sequence(conn, table)
    return counts


ANALYZE_TABLES: tuple[str, ...] = (
    "user",
    "media_file",
    "speaker_profile",
    "speaker",
    "transcript_segment",
    "task",
    "tag",
    "file_tag",
    "organization",
    "organization_membership",
    "content_hold",
    "content_hold_file",
    "collection",
    "collection_member",
    "collection_share",
    "speaker_cluster",
    "speaker_cluster_member",
    "speaker_match",
    "user_hidden_media",
    "chat_conversation",
    "chat_message",
    "comment",
)
