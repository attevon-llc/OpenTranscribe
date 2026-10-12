"""Deleting a file — by ANY route — must leave nothing behind in ANY store.

The question this answers is the operator's: "if data is deleted from the app, is it gone
from Postgres, OpenSearch, object storage and Redis?" Every test seeds a synthetic file
into every store a completed transcription occupies (``deletion_seed``), deletes it
through a real entry point, and then scans every store generically (``deletion_residue``)
for anything that survived. The scans are derived from the live schema and the live index
list, so a new table or index plane is covered without editing this file — and the
coverage guard fails if the seed stops populating a table the schema says depends on
``media_file``.

**Two transports, deliberately.** ``inprocess`` drives the FastAPI app of THIS checkout
through ``TestClient`` (real routing, middleware, cookie auth and CSRF), so it measures
the code under review. ``live`` sends the same requests to a running backend over the
network, so it measures the code that stack is actually serving. Both talk to the same
Postgres/OpenSearch/MinIO/Redis, which must be the stack named by ``POSTGRES_PORT`` etc.
The live leg refuses to run against a backend that cannot see the seeded rows (a mixed
stack), rather than reporting a meaningless pass.

Run against an isolated stack, never shared live data::

    POSTGRES_PORT=5376 MINIO_PORT=5378 OPENSEARCH_PORT=5380 REDIS_PORT=5377 \\
    DELETION_RESIDUE_BACKEND_URL=http://localhost:5374 \\
        pytest tests/integration/test_deletion_residue_live.py -m integration -o addopts=""

Everything a test creates is removed in ``finally`` (``teardown_seed``) even when the
delete under test leaked; the existing data on the stack is never addressed.
"""

from __future__ import annotations

import io
import os
import uuid as uuid_pkg
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.deletion_residue import CENTROID_MATCH_COSINE
from tests.integration.deletion_residue import FkEdge
from tests.integration.deletion_residue import centroid_residue
from tests.integration.deletion_residue import media_file_closure
from tests.integration.deletion_residue import minio_residue
from tests.integration.deletion_residue import opensearch_planes
from tests.integration.deletion_residue import opensearch_residue
from tests.integration.deletion_residue import redis_client
from tests.integration.deletion_residue import scan_residue
from tests.integration.deletion_residue import teardown_seed
from tests.integration.deletion_seed import DELRES_PREFIX
from tests.integration.deletion_seed import SeededChat
from tests.integration.deletion_seed import SeededFile
from tests.integration.deletion_seed import SharedCluster
from tests.integration.deletion_seed import create_throwaway_user
from tests.integration.deletion_seed import new_tag
from tests.integration.deletion_seed import seed_citing_chat
from tests.integration.deletion_seed import seed_complete_file
from tests.integration.deletion_seed import seed_shared_cluster

_STACK_ABSENT = (
    os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"
    or os.environ.get("SKIP_S3", "True").lower() == "true"
)

pytestmark = [
    pytest.mark.integration,
    # One writer at a time: a delete invalidates the owner's whole Redis cache
    # namespace, which would clear another test's seeded key out from under it.
    pytest.mark.xdist_group("deletion_residue"),
    pytest.mark.skipif(
        _STACK_ABSENT,
        reason="needs a reachable OpenSearch AND MinIO (export OPENSEARCH_PORT/MINIO_PORT)",
    ),
]

ADMIN_EMAIL = "admin@example.com"
# The documented shared dev/test identity; overridable for a stack seeded differently,
# the same convention as test_diar_native_*_live.py.
ADMIN_PASSWORD = os.environ.get("OT_TEST_ADMIN_PASSWORD", "password")


def _live_backend_url() -> str:
    return os.environ.get(
        "DELETION_RESIDUE_BACKEND_URL", os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")
    ).rstrip("/")


class Api:
    """A cookie-authenticated admin session over either transport (CSRF double-submit)."""

    def __init__(self, http: Any, base: str) -> None:
        self.http = http
        self.base = base

    def login(self, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> None:
        resp = self.http.post(
            f"{self.base}/api/auth/token", data={"username": email, "password": password}
        )
        assert resp.status_code == 200, f"admin login failed: {resp.status_code}"
        assert self.http.cookies.get("csrf_token"), "login set no csrf_token cookie"

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.http.cookies.get("csrf_token") or ""}

    def get(self, path: str) -> Any:
        return self.http.get(f"{self.base}{path}")

    def delete(self, path: str) -> Any:
        return self.http.delete(f"{self.base}{path}", headers=self._headers())

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self.http.post(f"{self.base}{path}", json=json or {}, headers=self._headers())

    def put(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self.http.put(f"{self.base}{path}", json=json or {}, headers=self._headers())

    def logout(self) -> None:
        self.http.post(f"{self.base}/api/auth/logout", headers=self._headers())


@pytest.fixture(params=["inprocess", "live"])
def api(request) -> Iterator[Api]:
    """An admin session against this checkout's app (``inprocess``) or a running one."""
    if request.param == "inprocess":
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as http:
            session = Api(http, "")
            session.login()
            try:
                yield session
            finally:
                session.logout()
        return

    import socket
    from urllib.parse import urlsplit

    import requests

    base = _live_backend_url()
    parts = urlsplit(base)
    try:
        socket.create_connection(
            (parts.hostname or "localhost", parts.port or 80), timeout=2
        ).close()
        listening = True
    except OSError:
        listening = False
    if not listening:
        pytest.skip(f"nothing listening at {base}; set DELETION_RESIDUE_BACKEND_URL")
    # A backend that IS listening but unhealthy is a failure, not a skip.
    requests.get(f"{base}/health", timeout=10).raise_for_status()
    live_http = requests.Session()
    session = Api(live_http, base)
    session.login()
    try:
        yield session
    finally:
        session.logout()


@pytest.fixture
def engine():
    """A plain engine on the stack's database — committed writes, no savepoint."""
    from app.core.config import settings

    eng = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def admin_id(engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text('SELECT id FROM "user" WHERE email = :e'), {"e": ADMIN_EMAIL}
            ).scalar_one()
        )


class Ledger:
    """What a test created, so the ``finally`` can remove it whatever happened."""

    def __init__(self) -> None:
        self.files: list[SeededFile] = []
        self.users: list[int] = []
        self.chats: list[int] = []
        # Profiles a test made through the API (a cluster promotion), not via the seed.
        self.profile_uuids: list[str] = []


@pytest.fixture
def created(engine) -> Iterator[Ledger]:
    ledger = Ledger()
    try:
        yield ledger
    finally:
        teardown_seed(
            engine,
            ledger.files,
            throwaway_user_ids=ledger.users,
            chat_conversation_ids=ledger.chats,
            api_profile_uuids=ledger.profile_uuids,
        )


def _seed(engine, created: Ledger, owner_id: int, **kwargs: Any) -> SeededFile:
    with Session(engine) as db:
        return seed_complete_file(db, owner_id, register=created.files.append, **kwargs)


def _chat(engine, created: Ledger, owner_id: int, files: list[SeededFile]) -> SeededChat:
    with Session(engine) as db:
        chat = seed_citing_chat(db, owner_id, files)
    created.chats.append(chat.conversation_id)
    return chat


def _closure(engine, sf: SeededFile) -> tuple[dict[str, set[Any]], dict[FkEdge, int]]:
    with engine.connect() as conn:
        return media_file_closure(conn, [sf.file_id])


def _assert_seed_reaches_every_store(engine, sf: SeededFile, *, include_owner: bool) -> tuple:
    """Coverage guard AND negative control: before the delete, every scan must FIRE.

    A residue scan that reports nothing before the delete would report nothing after it
    either, whatever the delete did — so each store's detector is proven live on this
    very file before its silence afterwards is accepted as evidence.
    """
    reached, edge_rows = _closure(engine, sf)
    unpopulated = sorted(f"{e.child}.{e.child_col}" for e, n in edge_rows.items() if n == 0)
    assert not unpopulated, (
        "the seed does not populate these tables that depend on media_file, so their "
        f"deletion is untested — extend deletion_seed.seed_complete_file: {unpopulated}"
    )
    planes = opensearch_planes(sf.file_uuid)
    assert {"chunk", "digest", "summary"} <= set(planes), f"index planes seeded: {planes}"
    with engine.connect() as conn:
        before = scan_residue(conn, sf, reached, edge_rows, include_owner=include_owner)
    for store in ("postgres", "opensearch", "minio", "redis"):
        assert any(f.startswith(f"{store}:") for f in before), (
            f"the {store} residue scan found nothing BEFORE the delete, so its silence "
            f"afterwards would prove nothing. Findings: {before}"
        )
    for idx in ("transcripts", "transcript_chunks"):
        assert any(idx in f for f in before), f"no {idx} documents seeded: {before}"
    return reached, edge_rows


def _assert_no_residue(engine, sf: SeededFile, reached, edge_rows, *, include_owner=False):
    with engine.connect() as conn:
        after = scan_residue(conn, sf, reached, edge_rows, include_owner=include_owner)
    assert after == [], f"deleting {sf.file_uuid} left residue:\n  " + "\n  ".join(after)


def _assert_foreign_citation_kept(engine, chat_message_id: int, foreign_uuid: str) -> None:
    """The scrub must remove only the deleted file's citations, not the whole history."""
    with engine.connect() as conn:
        citations = conn.execute(
            text("SELECT citations FROM chat_message WHERE id = :m"), {"m": chat_message_id}
        ).scalar_one()
    assert [c["file_uuid"] for c in citations] == [foreign_uuid]


def _assert_backend_sees(api: Api, sf: SeededFile) -> None:
    resp = api.get(f"/api/files/{sf.file_uuid}")
    assert resp.status_code == 200, (
        f"the backend at {api.base or 'in-process'} cannot see seeded file {sf.file_uuid} "
        f"({resp.status_code}) — it is not attached to the database at "
        f"POSTGRES_PORT={os.environ.get('POSTGRES_PORT')}; refusing to measure a mixed stack"
    )


# --------------------------------------------------------------------------- per-file routes


def test_single_delete_leaves_no_residue(api, engine, admin_id, created):
    sf = _seed(engine, created, admin_id)
    chat = _chat(engine, created, admin_id, [sf])
    _assert_backend_sees(api, sf)
    reached, edge_rows = _assert_seed_reaches_every_store(engine, sf, include_owner=False)

    resp = api.delete(f"/api/files/{sf.file_uuid}")
    assert resp.status_code == 204, resp.text

    _assert_no_residue(engine, sf, reached, edge_rows)
    _assert_foreign_citation_kept(engine, chat.message_id, chat.foreign_uuid)


def test_delete_of_a_requeued_file_leaves_no_residue(api, engine, admin_id, created):
    """A transcribed file put back in the queue is PENDING with every derived copy intact.

    Retry, reprocess and stuck-file recovery all reset a processed file to ``pending``,
    and ``DELETE /files/{uuid}`` used to route every pending file the caller owns to the
    lightweight "cancel an upload" branch, which removes only the original object and
    the row — leaving the transcript, chunks, voiceprints, thumbnail and rendition.
    """
    sf = _seed(engine, created, admin_id, status="pending")
    _assert_backend_sees(api, sf)
    reached, edge_rows = _assert_seed_reaches_every_store(engine, sf, include_owner=False)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT status FROM media_file WHERE id = :f"), {"f": sf.file_id}
            ).scalar_one()
            == "pending"
        ), "the seed must leave this file PENDING, or this test exercises the wrong branch"

    resp = api.delete(f"/api/files/{sf.file_uuid}")
    assert resp.status_code == 204, resp.text

    _assert_no_residue(engine, sf, reached, edge_rows)


def test_force_delete_leaves_no_residue(api, engine, admin_id, created):
    sf = _seed(engine, created, admin_id)
    _assert_backend_sees(api, sf)
    reached, edge_rows = _assert_seed_reaches_every_store(engine, sf, include_owner=False)

    resp = api.delete(f"/api/files/{sf.file_uuid}/force")
    assert resp.status_code == 200, resp.text

    _assert_no_residue(engine, sf, reached, edge_rows)


def test_bulk_delete_leaves_no_residue(api, engine, admin_id, created):
    files = [_seed(engine, created, admin_id) for _ in range(2)]
    chat = _chat(engine, created, admin_id, files)
    guards = [_assert_seed_reaches_every_store(engine, sf, include_owner=False) for sf in files]

    resp = api.post(
        "/api/files/management/bulk-action",
        json={"action": "delete", "file_uuids": [sf.file_uuid for sf in files]},
    )
    assert resp.status_code == 200, resp.text
    assert [r["success"] for r in resp.json()] == [True, True], resp.json()

    for sf, (reached, edge_rows) in zip(files, guards, strict=True):
        _assert_no_residue(engine, sf, reached, edge_rows)
    _assert_foreign_citation_kept(engine, chat.message_id, chat.foreign_uuid)


def test_retention_purge_leaves_no_residue(engine, admin_id, created):
    """The N-day retention sweep's per-file destroy, driven for ONE seeded file.

    In-process only: ``POST /admin/settings/retention-config/run`` sweeps every expired
    file on the stack, which on a shared stack would destroy real data.
    """
    from app.tasks.cleanup import _purge_expired_files

    sf = _seed(engine, created, admin_id)
    reached, edge_rows = _assert_seed_reaches_every_store(engine, sf, include_owner=False)

    assert _purge_expired_files([(sf.file_id, sf.file_uuid)]) == (1, 0)

    _assert_no_residue(engine, sf, reached, edge_rows)


# ------------------------------------------------------- surviving clusters keep no deleted voice


def _seed_shared_pair(
    engine, created: Ledger, owner_id: int, *, status: str = "completed"
) -> tuple[SeededFile, SeededFile, SharedCluster]:
    """File ``a`` (deleted by the test) and ``b`` (survives), one cluster over both."""
    a = _seed(engine, created, owner_id, status=status)
    b = _seed(engine, created, owner_id, voiceprint_offset=20)
    with Session(engine) as db:
        return a, b, seed_shared_cluster(db, a, b)


def _assert_centroid_still_averages_both(
    cluster: SharedCluster, a: SeededFile, b: SeededFile
) -> list[list[float]]:
    """Negative control: before the delete, the scan must FIRE on the two-member average.

    A centroid check that is silent before the delete would be silent after it whatever
    the delete did. Returns the surviving member's voiceprint, the expected centroid.
    """
    remaining = [cluster.voiceprints[b.speaker_uuids[0]]]
    assert centroid_residue(cluster.cluster_uuid, remaining), (
        "the centroid scan found nothing wrong with a centroid that averages in the file "
        "about to be deleted, so its silence afterwards would prove nothing"
    )
    return remaining


def _assert_centroid_rebuilt(
    engine, cluster: SharedCluster, remaining: list[list[float]], a: SeededFile
) -> None:
    assert centroid_residue(cluster.cluster_uuid, remaining) == [], (
        f"deleting {a.file_uuid} left the surviving cluster's centroid averaged with its voiceprint"
    )
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT member_count FROM speaker_cluster WHERE id = :c"),
            {"c": cluster.cluster_id},
        ).scalar_one()
    assert count == 1, "the surviving cluster's member_count was not brought in line"


@pytest.mark.parametrize("route", ["single", "pending", "force", "bulk"])
def test_surviving_cluster_centroid_forgets_a_deleted_file(api, engine, admin_id, created, route):
    a, b, cluster = _seed_shared_pair(
        engine, created, admin_id, status="pending" if route == "pending" else "completed"
    )
    _assert_backend_sees(api, a)
    remaining = _assert_centroid_still_averages_both(cluster, a, b)

    if route == "force":
        resp = api.delete(f"/api/files/{a.file_uuid}/force")
        assert resp.status_code == 200, resp.text
    elif route == "bulk":
        resp = api.post(
            "/api/files/management/bulk-action",
            json={"action": "delete", "file_uuids": [a.file_uuid]},
        )
        assert resp.status_code == 200, resp.text
        assert [r["success"] for r in resp.json()] == [True], resp.json()
    else:
        resp = api.delete(f"/api/files/{a.file_uuid}")
        assert resp.status_code == 204, resp.text

    _assert_centroid_rebuilt(engine, cluster, remaining, a)


def test_surviving_cluster_centroid_forgets_a_retention_purged_file(engine, admin_id, created):
    from app.tasks.cleanup import _purge_expired_files

    a, b, cluster = _seed_shared_pair(engine, created, admin_id)
    remaining = _assert_centroid_still_averages_both(cluster, a, b)

    assert _purge_expired_files([(a.file_id, a.file_uuid)]) == (1, 0)

    _assert_centroid_rebuilt(engine, cluster, remaining, a)


def test_account_deletion_leaves_no_centroid_of_the_accounts_clusters(
    api, engine, admin_id, created
):
    """An account's clusters die with it, so the shared cluster must not survive either."""
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service.aliases import get_active_speaker_index

    with Session(engine) as db:
        uid, user_uuid, _email = create_throwaway_user(db)
    created.users.append(uid)
    _a, _b, cluster = _seed_shared_pair(engine, created, uid)

    resp = api.delete(f"/api/admin/users/{user_uuid}")
    assert resp.status_code == 200, resp.text

    client = get_opensearch_client()
    assert client is not None
    assert not client.exists(
        index=get_active_speaker_index(), id=f"cluster_{cluster.cluster_uuid}"
    ), "the deleted account's shared-cluster centroid survived"


# ------------------------------------- a speaker leaving a cluster keeps no copy of its voice (#1214)


def _cosine(u: list[float], v: list[float]) -> float:
    import numpy as np

    a, b = np.array(u, dtype=np.float64), np.array(v, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def _voiceprint_docs(speaker_uuid: str) -> int:
    """How many speaker indices still hold this speaker's own voiceprint document."""
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    total = 0
    for idx in speaker_embedding_indices():
        if client.indices.exists(index=idx):
            client.indices.refresh(index=idx)
            body = {"query": {"ids": {"values": [speaker_uuid]}}}
            total += int(client.count(index=idx, body=body)["count"])
    return total


def _speaker_doc(doc_id: str) -> list[float] | None:
    """The embedding of a ``profile_*``/``cluster_*`` document, or None when absent."""
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service.aliases import get_active_speaker_index

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    index = get_active_speaker_index()
    if not client.exists(index=index, id=doc_id):
        return None
    return list(client.get(index=index, id=doc_id)["_source"]["embedding"])


def _link_to_profile(engine, sf: SeededFile, speaker_ids: list[int], voices: list[list[float]]):
    """Link speakers to ``sf``'s seeded profile; its embedding becomes their mean."""
    import numpy as np

    from app.services.opensearch_service import store_profile_embedding

    assert sf.profile_id is not None and sf.profile_uuid is not None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE speaker SET profile_id = :p WHERE id = ANY(:ids)"),
            {"p": sf.profile_id, "ids": speaker_ids},
        )
    mean = np.mean(np.array(voices, dtype=np.float64), axis=0)
    mean = (mean / np.linalg.norm(mean)).tolist()
    assert store_profile_embedding(
        sf.profile_id, sf.profile_uuid, f"{DELRES_PREFIX}{sf.tag}", mean, len(voices), sf.owner_id
    ), "seed: profile embedding was not stored"


def _remove_speaker_through(api: Api, engine, route: str, victim: str, keeper: str) -> None:
    if route == "speaker-delete":
        resp = api.delete(f"/api/speakers/{victim}")
        assert resp.status_code == 204, resp.text
    elif route == "segment-reassign":
        # Reassigning a speaker's last segment deletes the now-empty speaker.
        with engine.connect() as conn:
            segments = conn.execute(
                text(
                    "SELECT ts.uuid FROM transcript_segment ts JOIN speaker s "
                    "ON s.id = ts.speaker_id WHERE s.uuid = :u"
                ),
                {"u": victim},
            ).scalars()
            segment_uuids = [str(u) for u in segments]
        assert segment_uuids, "the victim speaker has no segments to reassign"
        for segment_uuid in segment_uuids:
            resp = api.put(
                f"/api/transcripts/segments/{segment_uuid}/speaker", json={"speaker_uuid": keeper}
            )
            assert resp.status_code == 200, resp.text
    else:
        resp = api.post(f"/api/speakers/{victim}/merge/{keeper}")
        assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("route", ["speaker-delete", "segment-reassign", "speaker-merge"])
def test_a_removed_speaker_leaves_no_copy_of_its_voice(api, engine, admin_id, created, route):
    """Deleting one speaker rebuilds every centroid and profile that averaged it in (#1214).

    The cluster holds one speaker from each of two files; the profile links the same two.
    Removing ``a``'s speaker through each entry point must leave the cluster's centroid and
    the profile's embedding equal to the SURVIVOR's voiceprint, ``member_count`` at 1, and
    (except for a merge, whose source voiceprint is folded into the target by the
    background task) no voiceprint document of the removed speaker anywhere.
    """
    a, b, cluster = _seed_shared_pair(engine, created, admin_id)
    _assert_backend_sees(api, a)
    victim, keeper = a.speaker_uuids
    survivor_voice = cluster.voiceprints[b.speaker_uuids[0]]
    remaining = _assert_centroid_still_averages_both(cluster, a, b)
    _link_to_profile(
        engine,
        a,
        [a.speaker_ids[0], b.speaker_ids[0]],
        [cluster.voiceprints[victim], survivor_voice],
    )
    before = _speaker_doc(f"profile_{a.profile_uuid}")
    assert before is not None and _cosine(before, survivor_voice) < CENTROID_MATCH_COSINE, (
        "control: the profile must average in the speaker about to be removed"
    )
    assert _voiceprint_docs(victim) >= 1, "control: the victim's voiceprint must be indexed"

    _remove_speaker_through(api, engine, route, victim, keeper)

    _assert_centroid_rebuilt(engine, cluster, remaining, a)
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM speaker WHERE uuid = :u"), {"u": victim}
        ).scalar_one()
    assert left == 0, f"{route} did not remove the speaker row"
    after = _speaker_doc(f"profile_{a.profile_uuid}")
    assert after is not None, "the profile kept a member, so it must keep an embedding"
    assert _cosine(after, survivor_voice) >= CENTROID_MATCH_COSINE, (
        f"{route} left the profile embedding averaged with the removed speaker's voice"
    )
    if route != "speaker-merge":
        assert _voiceprint_docs(victim) == 0, f"{route} left the removed speaker's voiceprint"


def test_unassigning_every_member_deletes_the_cluster_and_its_centroid(
    api, engine, admin_id, created
):
    a, b, cluster = _seed_shared_pair(engine, created, admin_id)
    assert _speaker_doc(f"cluster_{cluster.cluster_uuid}") is not None, "control: centroid seeded"

    resp = api.post(
        f"/api/speaker-clusters/{cluster.cluster_uuid}/unassign",
        json={"speaker_uuids": [a.speaker_uuids[0], b.speaker_uuids[0]], "blacklist": False},
    )
    assert resp.status_code == 200, resp.text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM speaker_cluster WHERE id = :c"), {"c": cluster.cluster_id}
        ).scalar_one()
    assert rows == 0, "an emptied cluster must be deleted"
    assert _speaker_doc(f"cluster_{cluster.cluster_uuid}") is None, (
        "the emptied cluster's centroid (an average of both voices) survived its row"
    )


@pytest.mark.parametrize("op", ["unassign", "split"])
def test_a_speaker_moved_out_of_a_cluster_leaves_its_centroid(api, engine, admin_id, created, op):
    a, b, cluster = _seed_shared_pair(engine, created, admin_id)
    victim = a.speaker_uuids[0]
    with engine.begin() as conn:  # the product keeps Speaker.cluster_id beside the membership
        conn.execute(
            text("UPDATE speaker SET cluster_id = :c WHERE id = ANY(:ids)"),
            {"c": cluster.cluster_id, "ids": [a.speaker_ids[0], b.speaker_ids[0]]},
        )
    remaining = _assert_centroid_still_averages_both(cluster, a, b)

    resp = api.post(
        f"/api/speaker-clusters/{cluster.cluster_uuid}/{op}",
        json={"speaker_uuids": [victim], **({"blacklist": False} if op == "unassign" else {})},
    )
    assert resp.status_code == 200, resp.text
    if op == "split":
        new_uuid = resp.json()["uuid"]
        with engine.connect() as conn:
            new_id = conn.execute(
                text("SELECT id FROM speaker_cluster WHERE uuid = :u"), {"u": new_uuid}
            ).scalar_one()
        b.extra_cluster_ids.append(int(new_id))
        b.extra_cluster_uuids.append(new_uuid)
        assert centroid_residue(new_uuid, [cluster.voiceprints[victim]]) == [], (
            "the split-off cluster's centroid is not its one member's voiceprint"
        )

    _assert_centroid_rebuilt(engine, cluster, remaining, a)


def test_deleting_a_cluster_erases_its_centroid_and_keeps_its_members(
    api, engine, admin_id, created
):
    a, b, cluster = _seed_shared_pair(engine, created, admin_id)
    assert _speaker_doc(f"cluster_{cluster.cluster_uuid}") is not None, "control: centroid seeded"

    resp = api.delete(f"/api/speaker-clusters/{cluster.cluster_uuid}")
    assert resp.status_code == 204, resp.text

    assert _speaker_doc(f"cluster_{cluster.cluster_uuid}") is None
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM speaker_cluster WHERE id = :c"),
                {"c": cluster.cluster_id},
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM speaker WHERE uuid = ANY(CAST(:u AS uuid[]))"),
                {"u": [a.speaker_uuids[0], b.speaker_uuids[0]]},
            ).scalar_one()
            == 2
        ), "deleting a grouping must not delete the speakers in it"


def test_deleting_a_promoted_profile_restores_member_labels(api, engine, admin_id, created):
    """A profile's name reached its members through promotion; deleting it takes it back."""
    a, b, cluster = _seed_shared_pair(engine, created, admin_id)
    with engine.begin() as conn:  # an unnamed cluster, so promotion names it after the profile
        conn.execute(
            text("UPDATE speaker_cluster SET label = NULL WHERE id = :c"), {"c": cluster.cluster_id}
        )
    name = f"{DELRES_PREFIX}{new_tag()}"
    resp = api.post(f"/api/speaker-clusters/{cluster.cluster_uuid}/promote", json={"name": name})
    assert resp.status_code == 200, resp.text
    profile_uuid = resp.json()["profile_uuid"]
    created.profile_uuids.append(profile_uuid)
    members = [a.speaker_uuids[0], b.speaker_uuids[0]]
    with engine.connect() as conn:
        labels = conn.execute(
            text("SELECT display_name FROM speaker WHERE uuid = ANY(CAST(:u AS uuid[]))"),
            {"u": members},
        ).scalars()
        assert set(labels) == {name}, "control: promotion names its members"
        assert (
            conn.execute(
                text("SELECT label FROM speaker_cluster WHERE id = :c"), {"c": cluster.cluster_id}
            ).scalar_one()
            == name
        ), "control: promotion names the cluster"

    resp = api.delete(f"/api/speaker-profiles/profiles/{profile_uuid}")
    assert resp.status_code == 204, resp.text

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT display_name, profile_id FROM speaker WHERE uuid = ANY(CAST(:u AS uuid[]))"
            ),
            {"u": members},
        ).all()
        label = conn.execute(
            text("SELECT label FROM speaker_cluster WHERE id = :c"), {"c": cluster.cluster_id}
        ).scalar_one()
    assert rows and all(r.display_name is None and r.profile_id is None for r in rows), rows
    assert label != name, "the cluster still carries the deleted profile's name"
    assert _speaker_doc(f"profile_{profile_uuid}") is None, "the deleted profile kept its embedding"


# ------------------------------- an unassigned speaker is no longer that person, anywhere (#1233)


def _as_throwaway_owner(api: Api, engine, created: Ledger) -> int:
    """Log ``api`` in as a fresh throwaway account and return its id.

    The assignment paths run a kNN over the OWNER's clusters, and the shared admin owns
    real data on a shared stack: a seeded voice must never be able to join one of those.
    """
    password = uuid_pkg.uuid4().hex
    with Session(engine) as db:
        uid, _user_uuid, email = create_throwaway_user(db, password=password)
    created.users.append(uid)
    api.logout()
    api.login(email, password)
    return uid


def _promote_shared(api: Api, engine, created: Ledger, cluster: SharedCluster) -> tuple[str, str]:
    with engine.begin() as conn:  # an unnamed cluster, so promotion names it after the profile
        conn.execute(
            text("UPDATE speaker_cluster SET label = NULL WHERE id = :c"), {"c": cluster.cluster_id}
        )
    name = f"{DELRES_PREFIX}{new_tag()}"
    resp = api.post(f"/api/speaker-clusters/{cluster.cluster_uuid}/promote", json={"name": name})
    assert resp.status_code == 200, resp.text
    created.profile_uuids.append(resp.json()["profile_uuid"])
    return resp.json()["profile_uuid"], name


def _speaker_row(engine, speaker_uuid: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, profile_id, display_name, cluster_id FROM speaker WHERE uuid = :u"),
            {"u": speaker_uuid},
        ).one()


def test_unassigning_from_a_promoted_cluster_unlinks_blacklists_and_sticks(api, engine, created):
    """The unassign rule end to end on real stores, then two assignment paths re-run.

    ``a``'s speaker leaves the promoted cluster: it loses the profile and the name that
    came with it, the blacklist is recorded both ways, and the cluster and profile stay
    for ``b`` with their centroid and embedding rebuilt from ``b`` alone. Then a batch
    assign (over the transport) and upload-time clustering (in-process, real kNN) must
    both leave it where the user put it.
    """
    from app.services.speaker_clustering_service import SpeakerClusteringService

    owner = _as_throwaway_owner(api, engine, created)
    a, b, cluster = _seed_shared_pair(engine, created, owner)
    profile_uuid, name = _promote_shared(api, engine, created, cluster)
    victim, keeper = a.speaker_uuids[0], b.speaker_uuids[0]
    assert _speaker_row(engine, victim).display_name == name, "control: promotion named it"
    keeper_voice = cluster.voiceprints[keeper]

    resp = api.post(
        f"/api/speaker-clusters/{cluster.cluster_uuid}/unassign",
        json={"speaker_uuids": [victim], "blacklist": True},
    )
    assert resp.status_code == 200, resp.text

    row = _speaker_row(engine, victim)
    assert (row.profile_id, row.display_name) == (None, None)
    with engine.connect() as conn:
        profile_id = conn.execute(
            text("SELECT id FROM speaker_profile WHERE uuid = :p"), {"p": profile_uuid}
        ).scalar_one()
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM speaker_profile_blacklist "
                    "WHERE speaker_id = :s AND profile_id = :p"
                ),
                {"s": row.id, "p": profile_id},
            ).scalar_one()
            == 1
        )
        cannot_link = {
            (int(r[0]), int(r[1]))
            for r in conn.execute(
                text(
                    "SELECT speaker_id, cannot_link_speaker_id FROM speaker_cannot_link "
                    "WHERE :s IN (speaker_id, cannot_link_speaker_id)"
                ),
                {"s": row.id},
            )
        }
        keeper_id = b.speaker_ids[0]
        assert {(row.id, keeper_id), (keeper_id, row.id)} <= cannot_link, cannot_link
        assert (
            conn.execute(
                text("SELECT profile_id FROM speaker WHERE id = :s"), {"s": keeper_id}
            ).scalar_one()
            == profile_id
        ), "the profile must remain for the other member"
    _assert_centroid_rebuilt(engine, cluster, [keeper_voice], a)
    profile_vector = _speaker_doc(f"profile_{profile_uuid}")
    assert profile_vector is not None and _cosine(profile_vector, keeper_voice) >= (
        CENTROID_MATCH_COSINE
    ), "the profile embedding still averages the unassigned voice"

    # Path 1, over the transport: a bulk assign is refused and says so.
    resp = api.post(
        "/api/speaker-clusters/batch-verify",
        json={"speaker_uuids": [victim], "action": "assign", "profile_uuid": profile_uuid},
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["updated_count"], resp.json()["failed_count"]) == (0, 1), resp.json()

    # Path 2, in-process with the real kNN: upload-time clustering of the victim's file.
    with Session(engine) as db:
        SpeakerClusteringService(db).cluster_speakers_for_file(a.file_id, owner)
    row = _speaker_row(engine, victim)
    if row.cluster_id is not None and row.cluster_id not in (cluster.cluster_id, a.cluster_id):
        with engine.connect() as conn:
            a.extra_cluster_uuids.append(
                str(
                    conn.execute(
                        text("SELECT uuid FROM speaker_cluster WHERE id = :c"),
                        {"c": row.cluster_id},
                    ).scalar_one()
                )
            )
        a.extra_cluster_ids.append(int(row.cluster_id))
    assert row.cluster_id != cluster.cluster_id, "clustering put the speaker back"
    assert row.profile_id is None, "clustering re-linked the unassigned speaker"
    assert _speaker_row(engine, a.speaker_uuids[1]).cluster_id is not None, (
        "control: the same pass clustered the file's other speaker, so it really ran"
    )


# --------------------------------------------------------------------------- teardown


def test_teardown_continues_past_a_failed_store_and_reports_every_failure(
    engine, admin_id, monkeypatch
):
    """A transient OpenSearch error must not leak the rows of every later step (#1233)."""
    from opensearchpy.exceptions import NotFoundError

    from app.services.opensearch_service import get_opensearch_client

    files: list[SeededFile] = []
    try:
        with Session(engine) as db:
            sf = seed_complete_file(db, admin_id, register=files.append)
        client = get_opensearch_client()
        assert client is not None
        calls: list[int] = []

        def search_context_missing(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            raise NotFoundError(404, "search_context_missing", {})

        monkeypatch.setattr(client, "delete_by_query", search_context_missing)

        raised: Exception | None = None
        try:
            teardown_seed(engine, files, throwaway_user_ids=[], chat_conversation_ids=[])
        except (NotFoundError, AssertionError) as exc:  # judged below, after the rows
            raised = exc

        with engine.connect() as conn:
            left = conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM tag WHERE id = :t)"
                    " + (SELECT count(*) FROM collection WHERE id = :c)"
                    " + (SELECT count(*) FROM speaker_profile WHERE id = :p)"
                    " + (SELECT count(*) FROM watch_source WHERE id = :w)"
                ),
                {
                    "t": sf.tag_id,
                    "c": sf.collection_id,
                    "p": sf.profile_id,
                    "w": sf.watch_source_id,
                },
            ).scalar_one()
        assert left == 0, f"{left} seeded row(s) survived: the steps after the failure never ran"
        from tests.integration.deletion_residue import TeardownIncompleteError

        assert isinstance(raised, TeardownIncompleteError), f"not reported as a whole: {raised!r}"
        assert f"opensearch documents of {sf.file_uuid}" in str(raised)
        assert len(calls) >= 3, "the scroll error is transient and must be retried first"
    finally:
        monkeypatch.undo()
        teardown_seed(engine, files, throwaway_user_ids=[], chat_conversation_ids=[])


# --------------------------------------------------------------------------- account routes


def _seed_account(engine, created: Ledger, admin_id: int) -> tuple[str, SeededFile, SeededChat]:
    """A throwaway account owning a full file, an avatar, and a chat; plus an admin chat
    that cites the account's file (a shared file's quotes live in OTHER users' history)."""
    from app.core.config import settings
    from app.services.minio_service import minio_client

    with Session(engine) as db:
        uid, user_uuid, _email = create_throwaway_user(db)
    created.users.append(uid)
    sf = _seed(engine, created, uid)
    avatar = f"avatars/{uid}/{sf.profile_uuid}.png"
    minio_client.put_object(settings.MEDIA_BUCKET_NAME, avatar, io.BytesIO(b"png"), 3, "image/png")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE speaker_profile SET avatar_path = :a WHERE id = :p"),
            {"a": avatar, "p": sf.profile_id},
        )
        # Every account that has opened Settings has rows here (ON DELETE CASCADE).
        conn.execute(
            text(
                "INSERT INTO user_setting (user_id, setting_key, setting_value) "
                "VALUES (:u, 'transcription_source_language', 'en')"
            ),
            {"u": uid},
        )
    _chat(engine, created, uid, [sf])
    return user_uuid, sf, _chat(engine, created, admin_id, [sf])


def _assert_account_gone(engine, sf: SeededFile, reached, edge_rows) -> None:
    with engine.connect() as conn:
        assert (
            conn.execute(
                text('SELECT count(*) FROM "user" WHERE id = :u'), {"u": sf.owner_id}
            ).scalar_one()
            == 0
        )
    _assert_no_residue(engine, sf, reached, edge_rows, include_owner=True)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("delete", "/api/admin/users/{uuid}", 200),
        ("delete", "/api/users/{uuid}", 204),
        ("post", "/api/admin/gdpr/erase-user/{uuid}", 200),
    ],
    ids=["admin-delete", "users-delete", "gdpr-erase"],
)
def test_account_deletion_leaves_no_residue(api, engine, admin_id, created, method, path, expected):
    user_uuid, sf, admin_chat = _seed_account(engine, created, admin_id)
    reached, edge_rows = _assert_seed_reaches_every_store(engine, sf, include_owner=True)

    resp = getattr(api, method)(path.format(uuid=user_uuid))
    assert resp.status_code == expected, resp.text

    _assert_account_gone(engine, sf, reached, edge_rows)
    _assert_foreign_citation_kept(engine, admin_chat.message_id, admin_chat.foreign_uuid)


# --------------------------------------------------------------------------- the detector


def test_the_scan_covers_an_index_it_was_never_told_about(engine, admin_id, created):
    """Negative control for the generic index scan, on a throwaway index.

    A document naming the file in an index the scanner has no knowledge of must be
    reported — that is what makes "no residue" mean every index, not a known list.
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    sf = SeededFile(
        owner_id=admin_id,
        file_id=-1,
        file_uuid=str(uuid_pkg.uuid4()),
        filename=f"{DELRES_PREFIX}{new_tag()}.wav",
        tag="negctl",
    )
    index = f"delres-negctl-{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(
        index=index, body={"mappings": {"properties": {"file_uuid": {"type": "keyword"}}}}
    )
    try:
        assert opensearch_residue(sf, include_owner=False) == []
        client.index(index=index, body={"file_uuid": sf.file_uuid}, refresh=True)
        assert opensearch_residue(sf, include_owner=False) == [f"opensearch: 1 doc(s) in {index}"]
    finally:
        client.indices.delete(index=index, ignore=[404])
    assert minio_residue(sf, include_owner=False) == []
    assert not redis_client(1).exists(f"cache:files:{admin_id}:{sf.tag}")
