"""Seed a synthetic, fully-processed media file into EVERY store a real one occupies.

Used by ``test_deletion_residue_live.py``. Nothing here runs a GPU: the transcript is
written straight to Postgres, and every OpenSearch plane is then built by the REAL
indexing task body (``index_transcript_search_task.apply``), which is what a completed
transcription runs. That is deliberate — a residue test whose fixtures hand-wrote the
index documents would only prove that the delete removes documents shaped like the
fixture's guess, not the ones production writes.

Everything is COMMITTED (no savepoint): the delete under test runs in another session —
or another process, for the live leg — and must see these rows. The caller owns cleanup;
``deletion_residue.teardown_seed`` removes whatever the delete did not, and is safe to
run twice.

Names carry ``DELRES_PREFIX`` + a uuid suffix so ``scripts/cleanup-test-data.py`` can
sweep a run that died before its teardown, and throwaway users carry
``DELRES_USER_PREFIX`` (registered in ``scripts/cleanup-test-users.py``).
"""

from __future__ import annotations

import io
import json
import uuid as uuid_pkg
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

DELRES_PREFIX = "e2e-delres-"
DELRES_USER_PREFIX = "delres-e2e-"

# Long, sentence-shaped turns: the extractive digest builder skips sentences too short to
# summarise, and a file with no digest would leave the digest plane untested.
_SCRIPT = [
    ("SPEAKER_00", "We reviewed the quarterly budget for the regional warehouse expansion today."),
    ("SPEAKER_01", "The contractor estimate came in eighteen percent above the approved ceiling."),
    ("SPEAKER_00", "Procurement will renegotiate the steel order before the end of the month."),
    ("SPEAKER_01", "If the steel price holds, the loading dock can still open in early spring."),
    ("SPEAKER_00", "Finance asked for a revised cash flow forecast covering the next two years."),
    ("SPEAKER_01", "Operations will hire two additional shift supervisors once the dock opens."),
    ("SPEAKER_00", "The safety audit flagged the north stairwell lighting as the top priority."),
    ("SPEAKER_01", "Maintenance already ordered replacement fixtures and expects them Thursday."),
]

_SUMMARY = {
    "bluf": "The warehouse expansion is over budget and steel will be renegotiated.",
    "major_topics": [
        {
            "topic": "Budget",
            "key_points": ["Contractor estimate is eighteen percent above the ceiling."],
        }
    ],
}


@dataclass
class SeededFile:
    """Identifiers of everything :func:`seed_complete_file` wrote, for scan and teardown."""

    owner_id: int
    file_id: int
    file_uuid: str
    filename: str
    tag: str
    speaker_ids: list[int] = field(default_factory=list)
    speaker_uuids: list[str] = field(default_factory=list)
    cluster_id: int | None = None
    cluster_uuid: str | None = None
    # Clusters this file's teardown must also remove: shared clusters seeded by
    # ``seed_shared_cluster`` that outlive a sibling file's deletion on purpose.
    extra_cluster_ids: list[int] = field(default_factory=list)
    extra_cluster_uuids: list[str] = field(default_factory=list)
    profile_id: int | None = None
    profile_uuid: str | None = None
    tag_id: int | None = None
    collection_id: int | None = None
    watch_source_id: int | None = None
    usage_event_id: str | None = None
    objects: list[tuple[str, str]] = field(default_factory=list)  # (bucket, key)
    redis_keys: list[tuple[int, str]] = field(default_factory=list)  # (db, key)


@dataclass
class SharedCluster:
    """A cluster holding one speaker from each of two seeded files, and its centroid."""

    cluster_id: int
    cluster_uuid: str
    # speaker uuid -> the voiceprint stored for it in OpenSearch
    voiceprints: dict[str, list[float]]


@dataclass
class SeededChat:
    """A conversation whose stored citations quote one or more seeded files."""

    conversation_id: int
    message_id: int
    foreign_uuid: str  # a citation that does NOT belong to any seeded file


def new_tag() -> str:
    """An 8-hex uniquifier, the shape the cleanup scripts' regexes require."""
    return uuid_pkg.uuid4().hex[:8]


def create_throwaway_user(db: Session, *, role: str = "user") -> tuple[int, str, str]:
    """Commit a throwaway local account. Returns ``(id, uuid, email)``."""
    from app.core.security import get_password_hash
    from app.models.user import User

    email = f"{DELRES_USER_PREFIX}{new_tag()}@example.com"
    user = User(
        email=email,
        full_name="Deletion residue subject",
        hashed_password=get_password_hash(uuid_pkg.uuid4().hex),
        is_active=True,
        is_superuser=False,
        role=role,
        auth_type="local",
    )
    db.add(user)
    db.commit()
    return int(user.id), str(user.uuid), email


def redis_client(db: int) -> Any:
    """A client for the stack's Redis, credentials from the environment."""
    import os

    import redis

    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "5177")),
        password=os.environ.get("REDIS_PASSWORD") or None,
        db=db,
        decode_responses=True,
        socket_timeout=5,
    )


def _put(bucket: str, key: str, payload: bytes, content_type: str) -> None:
    from app.services.minio_service import minio_client

    if not minio_client.bucket_exists(bucket):
        raise RuntimeError(f"bucket {bucket!r} is missing on the target MinIO")
    minio_client.put_object(bucket, key, io.BytesIO(payload), len(payload), content_type)


def _speaker_dimension() -> int:
    """The active speaker index's vector width, read from its mapping (v3 512 / v4 256)."""
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service.aliases import get_active_speaker_index

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    index = get_active_speaker_index()
    mapping = client.indices.get_mapping(index=index)
    props = next(iter(mapping.values()))["mappings"]["properties"]
    return int(props["embedding"]["dimension"])


def _unit_vector(dim: int, seed: int) -> list[float]:
    raw = [((i * 7919 + seed * 104729) % 997) / 997.0 + 0.01 for i in range(dim)]
    norm = sum(v * v for v in raw) ** 0.5
    return [v / norm for v in raw]


def seed_complete_file(  # noqa: C901 — one linear recipe; splitting it hides the inventory
    db: Session,
    owner_id: int,
    *,
    register: Callable[[SeededFile], None],
    status: str = "completed",
    with_profile: bool = True,
    voiceprint_offset: int = 0,
) -> SeededFile:
    """Write one completed file into Postgres, OpenSearch, MinIO and Redis.

    Args:
        db: A session bound to the live database. Committed as it goes.
        owner_id: Owning user id.
        register: Called with the :class:`SeededFile` as soon as the file row is
            committed, BEFORE anything else is written, so a seed that fails half-way
            is still torn down. Later steps fill in the same instance.
        status: ``FileStatus`` value to leave the row in. ``pending`` reproduces a file
            that was transcribed and then put back in the queue (retry / reprocess /
            recovery all do this) while every derived copy still exists.
        with_profile: Also create a speaker profile (blacklist target + embedding).
        voiceprint_offset: Shifts the speakers' synthetic voiceprints, so two seeded
            files do not share identical vectors (a centroid over identical members
            equals each member and could not tell the average from one of them).

    Returns:
        A :class:`SeededFile` naming everything written.
    """
    from app.core.config import settings
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment
    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import store_cluster_embedding
    from app.services.opensearch_service import store_profile_embedding
    from app.services.video_processing_service import VideoProcessingService

    tag = new_tag()
    filename = f"{DELRES_PREFIX}{tag}.wav"
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner_id,
        filename=filename,
        title=f"{DELRES_PREFIX}{tag} warehouse review",
        storage_path="pending",
        file_size=4096,
        content_type="audio/wav",
        duration=96.0,
        language="en",
        status=FileStatus.COMPLETED,
        completed_at=datetime.now(UTC),
        summary_data=dict(_SUMMARY),
        waveform_data={"1000": [0.1, 0.4, 0.2]},
    )
    db.add(media_file)
    db.flush()
    fid = int(media_file.id)
    base = f"user_{owner_id}/file_{fid}"
    media_file.storage_path = f"{base}/{filename}"
    media_file.thumbnail_path = f"{base}/thumbnail.webp"
    media_file.playback_path = f"{base}/{filename}.playback.m4a"

    speakers = []
    for label in ("SPEAKER_00", "SPEAKER_01"):
        speaker = Speaker(uuid=uuid_pkg.uuid4(), name=label, user_id=owner_id, media_file_id=fid)
        db.add(speaker)
        speakers.append(speaker)
    db.flush()
    by_label = {s.name: s for s in speakers}
    clock = 0.0
    for label, line in _SCRIPT:
        db.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=fid,
                speaker_id=by_label[label].id,
                start_time=clock,
                end_time=clock + 12.0,
                text=line,
            )
        )
        clock += 12.0
    db.commit()

    seeded = SeededFile(
        owner_id=owner_id,
        file_id=fid,
        file_uuid=str(media_file.uuid),
        filename=filename,
        tag=tag,
        speaker_ids=[int(s.id) for s in speakers],
        speaker_uuids=[str(s.uuid) for s in speakers],
    )
    register(seeded)
    s1, s2 = seeded.speaker_ids

    # Every remaining table with a foreign key into media_file / speaker. The residue
    # test's coverage guard derives that set from pg_constraint and fails if a new
    # table appears that this recipe does not populate.
    rows = db.execute(
        text(
            """
            WITH t AS (INSERT INTO tag (uuid, name, normalized_name, user_id, source)
                       VALUES (gen_random_uuid(), :name, :name, :uid, 'manual') RETURNING id),
                 c AS (INSERT INTO collection (uuid, name, user_id)
                       VALUES (gen_random_uuid(), :name, :uid) RETURNING id),
                 k AS (INSERT INTO speaker_cluster (uuid, user_id, label, member_count,
                                                    representative_speaker_id)
                       VALUES (gen_random_uuid(), :uid, :name, 2, :s1) RETURNING id, uuid),
                 w AS (INSERT INTO watch_source (uuid, name, source_type, is_enabled,
                                                 local_path, delete_after_import, s3_use_ssl,
                                                 smb_port, user_id, polling_interval_minutes,
                                                 use_fs_events, recursive, auto_transcribe,
                                                 multipart_enabled, multipart_regex,
                                                 multipart_time_window_hours,
                                                 multipart_wait_scans,
                                                 upload_stitched_to_source,
                                                 last_scan_files_found,
                                                 last_scan_files_imported,
                                                 last_scan_files_skipped,
                                                 total_files_imported)
                       VALUES (gen_random_uuid(), :name, 'local', false, '/nonexistent',
                               false, true, 445, :uid, 60, false, false, false, false, '',
                               1, 1, false, 0, 0, 0, 0) RETURNING id)
            SELECT (SELECT id FROM t), (SELECT id FROM c), (SELECT id FROM k),
                   (SELECT uuid FROM k), (SELECT id FROM w)
            """
        ),
        {"name": f"{DELRES_PREFIX}{tag}", "uid": owner_id, "s1": s1},
    ).one()
    seeded.tag_id, seeded.collection_id, seeded.cluster_id = (
        int(rows[0]),
        int(rows[1]),
        int(rows[2]),
    )
    seeded.cluster_uuid, seeded.watch_source_id = str(rows[3]), int(rows[4])

    if with_profile:
        prof = db.execute(
            text(
                "INSERT INTO speaker_profile (uuid, user_id, name) "
                "VALUES (gen_random_uuid(), :uid, :name) RETURNING id, uuid"
            ),
            {"uid": owner_id, "name": f"{DELRES_PREFIX}{tag}"},
        ).one()
        seeded.profile_id, seeded.profile_uuid = int(prof[0]), str(prof[1])

    params = {
        "fid": fid,
        "uid": owner_id,
        "s1": s1,
        "s2": s2,
        "tag_id": seeded.tag_id,
        "col": seeded.collection_id,
        "cl": seeded.cluster_id,
        "ws": seeded.watch_source_id,
        "fname": filename,
        "task": f"{DELRES_PREFIX}{tag}",
    }
    db.execute(text("INSERT INTO file_tag (media_file_id, tag_id) VALUES (:fid, :tag_id)"), params)
    db.execute(
        text(
            "INSERT INTO collection_member (uuid, collection_id, media_file_id) "
            "VALUES (gen_random_uuid(), :col, :fid)"
        ),
        params,
    )
    db.execute(
        text(
            "INSERT INTO comment (uuid, media_file_id, user_id, text) "
            "VALUES (gen_random_uuid(), :fid, :uid, 'Check the steel quote.')"
        ),
        params,
    )
    db.execute(
        text(
            "INSERT INTO analytics (uuid, media_file_id, overall_analytics) "
            "VALUES (gen_random_uuid(), :fid, '{\"talk_time\": {}}')"
        ),
        params,
    )
    db.execute(
        text(
            "INSERT INTO topic_suggestion (uuid, media_file_id, user_id, status) "
            "VALUES (gen_random_uuid(), :fid, :uid, 'pending')"
        ),
        params,
    )
    db.execute(
        text(
            "INSERT INTO file_pipeline_timing (task_id, file_id, user_id, created_at) "
            "VALUES (:task, :fid, :uid, now())"
        ),
        params,
    )
    db.execute(
        text(
            "INSERT INTO speaker_match (uuid, speaker1_id, speaker2_id, confidence) "
            "VALUES (gen_random_uuid(), :s1, :s2, 0.42)"
        ),
        params,
    )
    db.execute(
        text(
            "INSERT INTO speaker_cannot_link (speaker_id, cannot_link_speaker_id) VALUES (:s1, :s2)"
        ),
        params,
    )
    for sid in (s1, s2):
        db.execute(
            text(
                "INSERT INTO speaker_cluster_member (uuid, cluster_id, speaker_id) "
                "VALUES (gen_random_uuid(), :cl, :sid)"
            ),
            {**params, "sid": sid},
        )
    if seeded.profile_id is not None:
        db.execute(
            text(
                "INSERT INTO speaker_profile_blacklist (speaker_id, profile_id) VALUES (:s1, :pid)"
            ),
            {**params, "pid": seeded.profile_id},
        )
    db.execute(
        text(
            "INSERT INTO watch_source_file (uuid, watch_source_id, remote_path, filename, "
            "media_file_id, status, retry_count) "
            "VALUES (gen_random_uuid(), :ws, '/nonexistent/x.wav', :fname, :fid, 'imported', 0)"
        ),
        params,
    )
    seeded.usage_event_id = str(
        db.execute(
            text(
                "INSERT INTO usage_event (id, user_id, event_type, quantity, file_id) "
                "VALUES (gen_random_uuid(), :uid, 'transcription', 1, :fid) RETURNING id"
            ),
            params,
        ).scalar_one()
    )
    db.commit()

    # Object storage: original, thumbnail, playback rendition, and the derived cache —
    # both a known-name key and a policy-specific masked render that only a listing finds.
    media_bucket = settings.MEDIA_BUCKET_NAME
    for key, ctype in (
        (f"{base}/{filename}", "audio/wav"),
        (f"{base}/thumbnail.webp", "image/webp"),
        (f"{base}/{filename}.playback.m4a", "audio/mp4"),
    ):
        _put(media_bucket, key, b"delres", ctype)
        seeded.objects.append((media_bucket, key))
    video = VideoProcessingService.__new__(VideoProcessingService)
    video.cache_bucket = settings.CACHE_BUCKET_NAME
    derived = [VideoProcessingService.derived_cache_keys(video, fid, filename)[0]]
    stem = filename.rsplit(".", 1)[0]
    derived.append(
        f"{VideoProcessingService.DERIVED_CACHE_PREFIX}{fid}_{stem}_with_speakers_r0a1b2c3d.mp4"
    )
    for key in derived:
        _put(settings.CACHE_BUCKET_NAME, key, b"delres", "video/mp4")
        seeded.objects.append((settings.CACHE_BUCKET_NAME, key))

    # OpenSearch: the real indexing task writes the transcript doc and the chunk,
    # digest and summary planes, and creates the Task row and file_facts row on the way.
    from app.tasks.search_indexing_task import index_transcript_search_task

    result = index_transcript_search_task.apply(args=(fid, seeded.file_uuid, owner_id)).get()
    if result.get("status") != "success":
        raise RuntimeError(f"seed indexing did not succeed: {result!r}")

    dim = _speaker_dimension()
    for n, (sid, suuid) in enumerate(zip(seeded.speaker_ids, seeded.speaker_uuids, strict=True)):
        add_speaker_embedding(
            speaker_id=sid,
            speaker_uuid=suuid,
            user_id=owner_id,
            name=f"SPEAKER_0{n}",
            embedding=_unit_vector(dim, n + 1 + voiceprint_offset),
            media_file_id=fid,
        )
    if not store_cluster_embedding(
        seeded.cluster_uuid, owner_id, _unit_vector(dim, 9), label=f"{DELRES_PREFIX}{tag}"
    ):
        raise RuntimeError("seed: cluster centroid was not stored")
    if seeded.profile_id is not None and seeded.profile_uuid is not None:
        stored = store_profile_embedding(
            seeded.profile_id,
            seeded.profile_uuid,
            f"{DELRES_PREFIX}{tag}",
            _unit_vector(dim, 11),
            1,
            owner_id,
        )
        if not stored:
            raise RuntimeError("seed: profile embedding was not stored")

    # Redis: the owner's file-listing cache (db 1, ``redis_cache_service``), naming the file.
    cache_key = f"cache:files:{owner_id}:{DELRES_PREFIX}{tag}"
    seeded.redis_keys.append((1, cache_key))
    redis_client(1).setex(
        cache_key, 600, json.dumps([{"uuid": seeded.file_uuid, "filename": filename}])
    )

    # Last, because the indexing task's Task-row update re-derives the file's status.
    if status != FileStatus.COMPLETED.value:
        db.execute(
            text("UPDATE media_file SET status = :s WHERE id = :fid"),
            {"s": FileStatus(status).value, "fid": fid},
        )
        db.commit()
    return seeded


def seed_citing_chat(db: Session, owner_id: int, cited: list[SeededFile]) -> SeededChat:
    """A conversation (owned by ``owner_id``) whose persisted citations quote ``cited``.

    A citation stores a verbatim transcript snippet, so it is a copy of the file's text
    living in Postgres with no foreign key back to the file. A second citation naming a
    file that is NOT being deleted proves a scrub removes only the deleted file's entries.
    """
    foreign_uuid = str(uuid_pkg.uuid4())
    citations: list[dict[str, Any]] = [
        {
            "id": n + 1,
            "kind": "chunk",
            "file_uuid": sf.file_uuid,
            "title": sf.filename,
            "snippet": _SCRIPT[1][1],
        }
        for n, sf in enumerate(cited)
    ]
    citations.append(
        {"id": len(cited) + 1, "kind": "chunk", "file_uuid": foreign_uuid, "snippet": "x"}
    )
    row = db.execute(
        text(
            """
            WITH c AS (INSERT INTO chat_conversation (uuid, user_id, title)
                       VALUES (gen_random_uuid(), :uid, :title) RETURNING id)
            INSERT INTO chat_message (uuid, conversation_id, role, content, citations)
            SELECT gen_random_uuid(), c.id, 'assistant', 'See [1].', CAST(:cit AS jsonb) FROM c
            RETURNING conversation_id, id
            """
        ),
        {"uid": owner_id, "title": f"{DELRES_PREFIX}{new_tag()}", "cit": json.dumps(citations)},
    ).one()
    db.commit()
    return SeededChat(
        conversation_id=int(row[0]), message_id=int(row[1]), foreign_uuid=foreign_uuid
    )


def seed_shared_cluster(db: Session, a: SeededFile, b: SeededFile) -> SharedCluster:
    """One cluster over ``a``'s and ``b``'s first speakers, centroid = the mean voiceprint.

    The centroid is built exactly as production builds it (L2-normalised equal-weight mean
    of the members' stored voiceprints), so it differs from either member's own voiceprint
    — which is what lets a test tell "still averaged with the deleted file" from "rebuilt
    from the survivor". Registered on ``b`` for teardown, because ``a`` is the file the
    tests delete and ``b`` is the one whose cluster must outlive it.
    """
    import numpy as np

    from app.services.opensearch_service import get_speaker_embedding
    from app.services.opensearch_service import store_cluster_embedding

    assert a.owner_id == b.owner_id, "a cluster never spans owners"
    members = [(a.speaker_ids[0], a.speaker_uuids[0]), (b.speaker_ids[0], b.speaker_uuids[0])]
    voiceprints = {}
    for _sid, suuid in members:
        vector = get_speaker_embedding(suuid)
        assert vector is not None, f"seeded voiceprint for {suuid} is not readable"
        voiceprints[suuid] = vector

    row = db.execute(
        text(
            "INSERT INTO speaker_cluster (uuid, user_id, label, member_count, "
            "representative_speaker_id) VALUES (gen_random_uuid(), :uid, :label, 2, :rep) "
            "RETURNING id, uuid"
        ),
        {"uid": a.owner_id, "label": f"{DELRES_PREFIX}{b.tag}-shared", "rep": members[0][0]},
    ).one()
    cluster = SharedCluster(int(row[0]), str(row[1]), voiceprints)
    b.extra_cluster_ids.append(cluster.cluster_id)
    b.extra_cluster_uuids.append(cluster.cluster_uuid)
    for sid, _suuid in members:
        db.execute(
            text(
                "INSERT INTO speaker_cluster_member (uuid, cluster_id, speaker_id, confidence) "
                "VALUES (gen_random_uuid(), :cl, :sid, 0.9)"
            ),
            {"cl": cluster.cluster_id, "sid": sid},
        )
    db.commit()

    mean = np.mean(np.array(list(voiceprints.values()), dtype=np.float32), axis=0)
    mean = mean / np.linalg.norm(mean)
    if not store_cluster_embedding(
        cluster.cluster_uuid, a.owner_id, mean.tolist(), label=f"{DELRES_PREFIX}{b.tag}-shared"
    ):
        raise RuntimeError("seed: shared cluster centroid was not stored")
    return cluster
