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

from tests.integration.deletion_residue import FkEdge
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
from tests.integration.deletion_seed import create_throwaway_user
from tests.integration.deletion_seed import new_tag
from tests.integration.deletion_seed import seed_citing_chat
from tests.integration.deletion_seed import seed_complete_file

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
