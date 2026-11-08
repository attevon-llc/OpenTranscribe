"""Organizations, holds, collections, clusters, hidden media and chat for the #1337 seed.

Same rules as ``pg_synthetic``: one deterministic ``INSERT ... SELECT FROM generate_series`` per
table through ``insert().from_select``. A "spread" pairing is ``(start(parent) + q) % n``, where
``q`` counts the parent's rows, so a (parent, child) pair never repeats and a unique constraint
cannot trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import literal_column
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.expression import ColumnClause

from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.hidden_media import UserHiddenMedia
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerMatch
from app.scripts.scale_dataset.pg_activity import seed_activity
from app.scripts.scale_dataset.pg_synthetic import EMAIL_DOMAIN
from app.scripts.scale_dataset.pg_synthetic import SEEDED_TABLES
from app.scripts.scale_dataset.pg_synthetic import ClaimLedger
from app.scripts.scale_dataset.pg_synthetic import Shape
from app.scripts.scale_dataset.pg_synthetic import _claim_ids
from app.scripts.scale_dataset.pg_synthetic import _fill
from app.scripts.scale_dataset.pg_synthetic import _h
from app.scripts.scale_dataset.pg_synthetic import _table
from app.scripts.scale_dataset.pg_synthetic import _uuid
from app.scripts.scale_dataset.pg_synthetic import claim_core_bases
from app.scripts.scale_dataset.pg_synthetic import realign_sequences
from app.scripts.scale_dataset.pg_synthetic import seed_dataset

_ANCHOR = "timestamptz '2026-01-01 00:00:00+00'"


@dataclass(frozen=True)
class Bases:
    """Id offsets of the rows ``seed_dataset`` just wrote (new id = base + series index)."""

    user: int
    file: int
    speaker: int


def _lit(sql: str) -> ColumnClause[Any]:
    return literal_column(sql)


def _spread(parent: str, per_parent_index: str, size: int, seed: str, salt: str) -> str:
    """1-based child index in ``[1, size]``; distinct across ``per_parent_index`` for one parent."""
    start = f"(hashtext('{seed}:{salt}:' || {parent}::text)::bigint & 2147483647)"
    return f"(1 + ({start} + {per_parent_index}) % {size})"


def seed_governance(conn: Connection, shape: Shape, seed: str, b: Bases) -> dict[str, int]:
    """Insert the organization, hold, collection, cluster, hidden-media and chat rows."""
    counts: dict[str, int] = {}
    g = "g"

    def h(salt: str) -> str:
        return _h(seed, salt, g)

    def other_user(salt: str) -> str:
        return f"({b.user} + 3 + {h(salt)} % {shape.users - 2})"

    base_org = _claim_ids(conn, _table(Organization), shape.max_rows())
    counts["organization"] = _fill(
        conn,
        _table(Organization),
        shape.orgs,
        {
            "id": _lit(f"{base_org} + g"),
            "uuid": _uuid(seed, "org", g),
            "name": _lit("'Perf Org ' || g::text"),
            "slug": _lit(f"'perf-org-{seed}-' || g::text"),
            "is_active": _lit("true"),
        },
    )
    # Users 3.. belong to org ((i - 3) % orgs) + 1; user 3 is that org's admin (perf-3).
    mem = shape.users - 2
    base_mem = _claim_ids(conn, _table(OrganizationMembership), shape.max_rows())
    counts["organization_membership"] = _fill(
        conn,
        _table(OrganizationMembership),
        mem,
        {
            "id": _lit(f"{base_mem} + g"),
            "organization_id": _lit(f"{base_org} + 1 + ((g - 1) % {shape.orgs})"),
            "user_id": _lit(f"{b.user} + 2 + g"),
            "role": _lit("CASE WHEN g <= 1 THEN 'org:admin' ELSE 'org:member' END"),
        },
    )

    base_hold = _claim_ids(conn, _table(ContentHold), shape.max_rows())
    plat = f"(((g - 1) * 1000 / {shape.holds}) % 1000 < {shape.platform_hold_pm})"
    counts["content_hold"] = _fill(
        conn,
        _table(ContentHold),
        shape.holds,
        {
            "id": _lit(f"{base_hold} + g"),
            "uuid": _uuid(seed, "hold", g),
            "authority": _lit(f"CASE WHEN {plat} THEN 'platform' ELSE 'organization' END"),
            "organization_id": _lit(
                f"CASE WHEN {plat} THEN NULL ELSE {base_org} + 1 + (g % {shape.orgs}) END"
            ),
            "scope": _lit("'file'"),
            "reason": _lit("'loadtest hold ' || g::text || ' for query review'"),
            "placed_by_user_id": _lit(f"{b.user} + 1"),
            "placed_at": _lit(f"{_ANCHOR} - (g || ' hours')::interval"),
            "released_at": _lit(
                f"CASE WHEN {h('hrel')} % 1000 < {shape.released_hold_pm} "
                f"THEN {_ANCHOR} - (g || ' minutes')::interval END"
            ),
        },
    )
    hq = f"((g - 1) / {shape.holds})"
    hold_idx = f"((g - 1) % {shape.holds} + 1)"
    counts["content_hold_file"] = _fill(
        conn,
        _table(ContentHoldFile),
        shape.hold_files,
        {
            "hold_id": _lit(f"{base_hold} + {hold_idx}"),
            "media_file_id": _lit(f"{b.file} + {_spread(hold_idx, hq, shape.files, seed, 'hf')}"),
            "added_at": _lit(f"{_ANCHOR}"),
        },
    )

    base_col = _claim_ids(conn, _table(Collection), shape.max_rows())
    org_col = f"({h('corg')} % 1000 < {shape.org_collection_pm})"
    owner = other_user("cown")
    counts["collection"] = _fill(
        conn,
        _table(Collection),
        shape.collections,
        {
            "id": _lit(f"{base_col} + g"),
            "uuid": _uuid(seed, "collection", g),
            "name": _lit("'Perf collection ' || g::text"),
            "user_id": _lit(owner),
            "organization_id": _lit(
                f"CASE WHEN {org_col} THEN {base_org} + 1 + ({owner} - {b.user} - 3) % {shape.orgs} END"
            ),
            "is_public": _lit("false"),
            "visibility": _lit(
                f"CASE WHEN {org_col} AND g % 2 = 1 THEN 'restricted' ELSE 'organization' END"
            ),
        },
    )
    cq = f"((g - 1) / {shape.collections})"
    col_idx = f"((g - 1) % {shape.collections} + 1)"
    base_cm = _claim_ids(conn, _table(CollectionMember), shape.max_rows())
    counts["collection_member"] = _fill(
        conn,
        _table(CollectionMember),
        shape.collection_members,
        {
            "id": _lit(f"{base_cm} + g"),
            "uuid": _uuid(seed, "cmember", g),
            "collection_id": _lit(f"{base_col} + {col_idx}"),
            "media_file_id": _lit(f"{b.file} + {_spread(col_idx, cq, shape.files, seed, 'cm')}"),
        },
    )
    base_cs = _claim_ids(conn, _table(CollectionShare), shape.max_rows())
    counts["collection_share"] = _fill(
        conn,
        _table(CollectionShare),
        shape.collection_shares,
        {
            "id": _lit(f"{base_cs} + g"),
            "uuid": _uuid(seed, "cshare", g),
            "collection_id": _lit(f"{base_col} + 1 + (g - 1) % {shape.collections}"),
            "shared_by_id": _lit(f"{b.user} + 2"),
            "target_type": _lit("'user'"),
            "target_user_id": _lit(other_user("cshu")),
            "permission": _lit("'viewer'"),
        },
    )

    base_cl = _claim_ids(conn, _table(SpeakerCluster), shape.max_rows())
    counts["speaker_cluster"] = _fill(
        conn,
        _table(SpeakerCluster),
        shape.clusters,
        {
            "id": _lit(f"{base_cl} + g"),
            "uuid": _uuid(seed, "cluster", g),
            "user_id": _lit(other_user("clu")),
            "member_count": _lit(f"{shape.cluster_members} / {shape.clusters}"),
            "quality_score": _lit(f"({h('cq')} % 1000) / 1000.0"),
        },
    )
    spk = shape.files * shape.speakers_per_file
    kq = f"((g - 1) / {shape.clusters})"
    cl_idx = f"((g - 1) % {shape.clusters} + 1)"
    base_cmem = _claim_ids(conn, _table(SpeakerClusterMember), shape.max_rows())
    counts["speaker_cluster_member"] = _fill(
        conn,
        _table(SpeakerClusterMember),
        shape.cluster_members,
        {
            "id": _lit(f"{base_cmem} + g"),
            "uuid": _uuid(seed, "clmember", g),
            "cluster_id": _lit(f"{base_cl} + {cl_idx}"),
            "speaker_id": _lit(f"{b.speaker} + {_spread(cl_idx, kq, spk, seed, 'clm')}"),
        },
    )
    base_match = _claim_ids(conn, _table(SpeakerMatch), shape.max_rows())
    # (s, s + 1 + q): unique per (s, q) and always speaker1 < speaker2.
    mq = f"((g - 1) / {spk - 1})"
    s1 = f"((g - 1) % {spk - 1} + 1)"
    counts["speaker_match"] = _fill(
        conn,
        _table(SpeakerMatch),
        shape.matches,
        {
            "id": _lit(f"{base_match} + g"),
            "uuid": _uuid(seed, "match", g),
            "speaker1_id": _lit(f"{b.speaker} + {s1}"),
            "speaker2_id": _lit(f"{b.speaker} + {s1} + 1 + {mq}"),
            "confidence": _lit(f"0.5 + ({h('mc')} % 500) / 1000.0"),
        },
    )

    counts["user_hidden_media"] = _fill(
        conn,
        _table(UserHiddenMedia),
        shape.hidden,
        {
            "user_id": _lit(f"{b.user} + 2 + (g - 1) % 2"),
            "media_file_id": _lit(f"{b.file} + g"),
        },
    )

    base_conv = _claim_ids(conn, _table(ChatConversation), shape.max_rows())
    counts["chat_conversation"] = _fill(
        conn,
        _table(ChatConversation),
        shape.conversations,
        {
            "id": _lit(f"{base_conv} + g"),
            "uuid": _uuid(seed, "conv", g),
            "user_id": _lit(other_user("chu")),
            "title": _lit("'Perf chat ' || g::text"),
            "is_archived": _lit("false"),
            "last_message_at": _lit(f"{_ANCHOR} - ((g % 365) || ' days')::interval"),
        },
    )
    base_msg = _claim_ids(conn, _table(ChatMessage), shape.max_rows())
    cited = f"md5('{seed}:file:' || (1 + {h('cf')} % {shape.files})::text)::uuid::text"
    counts["chat_message"] = _fill(
        conn,
        _table(ChatMessage),
        shape.messages,
        {
            "id": _lit(f"{base_msg} + g"),
            "uuid": _uuid(seed, "msg", g),
            "conversation_id": _lit(f"{base_conv} + 1 + (g - 1) % {shape.conversations}"),
            "role": _lit("CASE WHEN g % 2 = 1 THEN 'user' ELSE 'assistant' END"),
            "content": _lit("'loadtest message ' || g::text"),
            "citations": _lit(
                f"CASE WHEN g % 2 = 0 THEN jsonb_build_array("
                f"jsonb_build_object('id', 1, 'file_uuid', {cited}, 'start_time', 0)) END"
            ),
            "status": _lit("'complete'"),
            "created_at": _lit(f"{_ANCHOR} - ((g % 365) || ' days')::interval"),
        },
    )
    return counts


def seed_all(
    conn: Connection,
    shape: Shape,
    seed: str,
    password_hash: str,
    ledger: ClaimLedger | None = None,
) -> dict[str, int]:
    """The whole dataset: core tables, governance tables, then the load test's activity rows."""
    claimed = claim_core_bases(conn, shape, ledger)
    bases = Bases(user=claimed[0], file=claimed[1], speaker=claimed[2])
    counts = seed_dataset(conn, shape, seed, password_hash, claimed, ledger)
    counts.update(seed_governance(conn, shape, seed, bases))
    counts.update(seed_activity(conn, shape, seed, bases.user, bases.file, ledger))
    realign_sequences(conn, SEEDED_TABLES)
    return counts


def seed_activity_only(
    conn: Connection, shape: Shape, seed: str, ledger: ClaimLedger | None = None
) -> dict[str, int]:
    """Add only the load test's activity rows to a database that already holds the seed.

    The users and files exist (a restored snapshot), so ``seed_all`` would trip the unique
    email. The id bases are read back from the seeded rows: the first seeded user is
    ``perf-1`` and the first seeded file is the lowest id owned by a seeded user.
    """
    base_user = conn.execute(
        text('SELECT id - 1 FROM "user" WHERE email = :email'),
        {"email": f"perf-1@{EMAIL_DOMAIN}"},
    ).scalar_one_or_none()
    if base_user is None:
        raise ValueError("no seeded users in this database; run the full seed first")
    base_file = conn.execute(
        text(
            'SELECT min(m.id) - 1 FROM media_file m JOIN "user" u ON u.id = m.user_id '
            "WHERE u.email LIKE :pattern"
        ),
        {"pattern": f"perf-%@{EMAIL_DOMAIN}"},
    ).scalar_one()
    present = conn.execute(
        text("SELECT count(*) FROM comment WHERE text LIKE 'loadtest comment %'")
    ).scalar_one()
    if present:
        return {}
    counts = seed_activity(conn, shape, seed, base_user, base_file, ledger)
    # An older snapshot left sequences behind max(id) (M2 T3); repair them while it is open.
    realign_sequences(conn, SEEDED_TABLES)
    return counts
