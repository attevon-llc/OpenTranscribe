"""The #1337 seed's governance tables: organizations, holds, collections, clusters, chat.

Counts, the constraint-sensitive properties (hold authority split, perf-3 as org admin, citations
that point at seeded files) and determinism over every new table.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.engine import Connection

from app.scripts.scale_dataset import pg_synthetic as pg
from app.scripts.scale_dataset.pg_governance import seed_all

SHAPE = pg.Shape(
    users=6,
    files=20,
    heavy_user_files=8,
    speakers_per_file=10,
    segments_per_file=2,
    tasks_per_file=1,
    profiles=4,
    tags=6,
    tags_per_file=3,
    orgs=2,
    holds=4,
    hold_files=8,
    released_hold_pm=500,
    platform_hold_pm=500,
    collections=5,
    collection_members=15,
    collection_shares=3,
    clusters=4,
    cluster_members=12,
    matches=30,
    hidden=6,
    conversations=3,
    messages=9,
    comments=10,
    user_tags=4,
)
NEW = {
    "organization": 2,
    "organization_membership": 4,
    "content_hold": 4,
    "content_hold_file": 8,
    "collection": 5,
    "collection_member": 15,
    "collection_share": 3,
    "speaker_cluster": 4,
    "speaker_cluster_member": 12,
    "speaker_match": 30,
    "user_hidden_media": 6,
    "chat_conversation": 3,
    "chat_message": 9,
}
# table -> columns that are a pure function of the seed (server-default timestamps left out)
FINGERPRINT = {
    "organization": ("uuid", "name", "slug"),
    "organization_membership": ("organization_id", "user_id", "role"),
    "content_hold": ("uuid", "authority", "organization_id", "reason", "released_at"),
    "content_hold_file": ("hold_id", "media_file_id"),
    "collection": ("uuid", "user_id", "organization_id", "visibility", "name"),
    "collection_member": ("collection_id", "media_file_id"),
    "collection_share": ("collection_id", "target_user_id", "permission"),
    "speaker_cluster": ("uuid", "user_id", "member_count", "quality_score"),
    "speaker_cluster_member": ("cluster_id", "speaker_id"),
    "speaker_match": ("speaker1_id", "speaker2_id", "confidence"),
    "user_hidden_media": ("user_id", "media_file_id"),
    "chat_conversation": ("uuid", "user_id", "title", "last_message_at"),
    "chat_message": ("uuid", "conversation_id", "role", "content", "citations"),
}


def _fingerprint(conn: Connection, seed: str) -> dict[str, str]:
    """md5 per table over rows reachable from this seed's files/users (ids are not compared)."""
    from app.db.base import Base

    marker = f"loadtest/{seed}/%"
    seeded_files = select(Base.metadata.tables["media_file"].c.id).where(
        Base.metadata.tables["media_file"].c.storage_path.like(marker)
    )
    out: dict[str, str] = {}
    for name, cols in FINGERPRINT.items():
        table = Base.metadata.tables[name]
        row = func.concat_ws("|", *[cast(table.c[c], Text) for c in cols])
        agg = func.string_agg(row, aggregate_order_by(text("','"), row))
        stmt = select(func.md5(func.coalesce(agg, "")))
        if "media_file_id" in table.c:
            stmt = stmt.where(table.c.media_file_id.in_(seeded_files))
        elif "slug" in table.c:
            stmt = stmt.where(table.c.slug.like(f"perf-org-{seed}-%"))
        elif name == "organization_membership":
            stmt = stmt.where(
                table.c.organization_id.in_(
                    select(Base.metadata.tables["organization"].c.id).where(
                        Base.metadata.tables["organization"].c.slug.like(f"perf-org-{seed}-%")
                    )
                )
            )
        else:
            stmt = stmt.where(
                table.c.uuid.in_(_uuids(conn, name, seed)) if "uuid" in table.c else text("true")
            )
        out[name] = conn.execute(stmt).scalar_one()
    return out


def _uuids(conn: Connection, name: str, seed: str):
    """Subquery of the uuids this seed generated for ``name``: md5(seed:salt:g) with g = 1..n."""
    salt = {
        "content_hold": "hold",
        "collection": "collection",
        "speaker_cluster": "cluster",
        "chat_conversation": "conv",
        "chat_message": "msg",
    }.get(name)
    if salt is None:
        return text("select null::uuid where false")
    n = NEW[name]
    return text(f"select md5('{seed}:{salt}:' || g::text)::uuid from generate_series(1, {n}) g")


def _seed(db_session, seed: str = "g1337"):
    conn = db_session.connection()
    counts = seed_all(conn, SHAPE, seed, "x-not-a-real-hash")
    return counts, _fingerprint(conn, seed)


def test_every_new_table_gets_the_requested_row_count(db_session):
    counts, _ = _seed(db_session)

    assert {k: counts[k] for k in NEW} == NEW


def test_holds_use_both_authorities_and_perf3_is_an_org_admin(db_session):
    _seed(db_session)
    conn = db_session.connection()

    holds = dict(
        conn.execute(
            text(
                "SELECT authority, count(*) FROM content_hold "
                "WHERE reason LIKE 'loadtest hold %' GROUP BY authority"
            )
        ).all()
    )
    admin = conn.execute(
        text(
            'SELECT m.role FROM organization_membership m JOIN "user" u ON u.id = m.user_id '
            "WHERE u.email = 'perf-3@loadtest.invalid'"
        )
    ).scalar_one()
    cited = conn.execute(
        text(
            "SELECT count(*) FROM chat_message c WHERE c.citations IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM media_file m WHERE m.uuid::text = c.citations->0->>'file_uuid')"
        )
    ).scalar_one()

    assert set(holds) == {"organization", "platform"}, holds
    assert admin == "org:admin"
    assert cited == 4  # the assistant rows (even g) of 9 messages


def test_the_app_can_insert_after_the_seed_in_every_seeded_table(db_session):
    """Explicit-id inserts leave the id sequence behind; the next app INSERT must not collide.

    The load test creates collections, comments and chats on a seeded stack, so a sequence
    still at 1 turns every such request into a duplicate-key 500.
    """
    from app.db.base import Base

    counts, _ = _seed(db_session)
    conn = db_session.connection()
    behind = []
    for name in counts:
        table = Base.metadata.tables.get(name)
        if table is None or "id" not in table.c:  # a count that is not a table, or a link table
            continue
        seq = conn.execute(
            text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": f'"{name}"'}
        ).scalar_one_or_none()
        if seq is None:  # a non-serial id (task ids are strings)
            continue
        top = conn.execute(text(f'SELECT max(id) FROM "{name}"')).scalar_one()
        last = conn.execute(text(f"SELECT last_value FROM {seq}")).scalar_one()
        if top is not None and last < top:
            behind.append(f"{name}: sequence {last} < max(id) {top}")

    assert not behind, behind
    assert "collection" in counts and "chat_conversation" in counts  # the checked tables ran


def test_two_seeds_generate_identical_governance_rows(db_session):
    first_counts, first = _seed(db_session)
    db_session.rollback()
    second_counts, second = _seed(db_session)

    assert first_counts == second_counts
    assert first == second
    assert set(first) == set(NEW)
    assert len(set(first.values())) > 1
