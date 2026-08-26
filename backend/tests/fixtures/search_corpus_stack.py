"""Live-stack pytest fixtures that seed the self-contained search-quality corpus.

Owns a throwaway ``searchqual-<uuid8hex>@example.invalid`` user, injects the six
:mod:`tests.fixtures.search_corpus` meetings via the production corpus-injection
tool (real chunking/embedding/indexing, no ASR), and tears everything down —
OpenSearch refreshed BEFORE the delete, per
``tests/integration/test_corpus_injection_synthetic_e2e.py``'s ordering, so
freshly indexed chunks are never orphaned.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

BASE = f"http://localhost:{os.environ.get('BACKEND_PORT', '5174')}/api"
SEARCH_CORPUS_PASSWORD = "search-quality-fixture-pw-1"  # noqa: S105 — throwaway test user only


@pytest.fixture(scope="session")
def search_corpus_user():
    """Create the throwaway corpus-owning user; delete it (and its files) on teardown."""
    from app.core.security import get_password_hash
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment
    from app.models.user import User

    email = f"searchqual-{uuid.uuid4().hex[:8]}@example.invalid"
    db = SessionLocal()
    try:
        user = User(
            email=email,
            full_name="Search Quality Fixture",
            hashed_password=get_password_hash(SEARCH_CORPUS_PASSWORD),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id

        yield {"id": user_id, "email": email, "password": SEARCH_CORPUS_PASSWORD}

        # Backstop only — `search_corpus`'s own teardown (it depends on this
        # fixture, so its teardown runs first) already deletes segments/speakers/
        # media rows. This exists so a failure that skips that teardown (e.g. the
        # `search_corpus` fixture never ran, or its setup raised before yielding)
        # still leaves nothing behind, without hitting the FK from
        # transcript_segment/speaker onto media_file.
        file_ids = [
            row[0] for row in db.query(MediaFile.id).filter(MediaFile.user_id == user_id).all()
        ]
        if file_ids:
            db.query(TranscriptSegment).filter(
                TranscriptSegment.media_file_id.in_(file_ids)
            ).delete(synchronize_session=False)
            db.query(Speaker).filter(Speaker.media_file_id.in_(file_ids)).delete(
                synchronize_session=False
            )
            db.query(MediaFile).filter(MediaFile.id.in_(file_ids)).delete(synchronize_session=False)
            db.commit()
        reloaded = db.get(User, user_id)
        if reloaded is not None:
            db.delete(reloaded)
            db.commit()
    finally:
        db.close()


def _wait_for_indexed(file_uuids: list[str], timeout: float = 60.0) -> None:
    """Poll OpenSearch directly (not ``/api/search``) until every file has chunks.

    Deliberately bypasses the app's search response cache
    (``SEARCH_CACHE_TTL_SECONDS`` = 300s, ``hybrid_search_service.py``): polling
    ``GET /api/search`` with the same query/user/page params repeatedly would hit
    that cache and could pin a "not yet fully indexed" 4-of-6 result for five
    minutes, well past this function's own timeout — the exact self-inflicted
    flake this fixture exists to avoid. Reading OpenSearch directly checks the
    thing that's actually converging.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import chunk_plane_query

    client = get_opensearch_client()
    if client is None:
        raise AssertionError("OpenSearch client unavailable — cannot verify corpus indexing")

    deadline = time.monotonic() + timeout
    seen: set[str] = set()
    while time.monotonic() < deadline:
        client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
        seen = set()
        for file_uuid in file_uuids:
            hits = client.count(
                index=settings.OPENSEARCH_CHUNKS_INDEX, body={"query": chunk_plane_query(file_uuid)}
            )
            if hits.get("count", 0) > 0:
                seen.add(file_uuid)
        if seen == set(file_uuids):
            return
        time.sleep(1.0)
    missing = set(file_uuids) - seen
    raise AssertionError(
        f"search_corpus did not converge in {timeout}s: {len(missing)} of {len(file_uuids)} "
        f"files never got chunks indexed: {sorted(missing)}. A half-indexed corpus must not "
        "silently produce downstream test failures."
    )


@pytest.fixture(scope="session")
def search_corpus_token(search_corpus_user):
    """Log the throwaway user in via the real login endpoint; return the bearer token."""
    resp = requests.post(
        f"{BASE}/auth/login",
        data={
            "username": search_corpus_user["email"],
            "password": search_corpus_user["password"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def search_corpus(search_corpus_user, search_corpus_token):
    """Inject all six meetings, index them for real, wait for convergence, then tear down."""
    from app.core.config import settings
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment
    from app.scripts.corpus_injection.injector import dispatch_indexing
    from app.scripts.corpus_injection.injector import inject_meeting
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import TranscriptIndexingService
    from tests.fixtures.search_corpus import build_meeting_docs

    user_id = search_corpus_user["id"]
    seed = f"pytest-searchqual-{uuid.uuid4().hex[:10]}"

    db = SessionLocal()
    records = {}
    try:
        for doc in build_meeting_docs():
            record, _ = inject_meeting(db, doc, user_id, seed=seed, tool_version="pytest")
            db.commit()
            dispatch_indexing(record, user_id, mode="eager")
            records[doc.meeting_id] = record
    finally:
        db.close()

    _wait_for_indexed([r.file_uuid for r in records.values()])

    meeting_id_to_file_uuid = {mid: r.file_uuid for mid, r in records.items()}
    file_uuid_to_meeting_id = {r.file_uuid: mid for mid, r in records.items()}

    yield {
        "records": records,
        "meeting_id_to_file_uuid": meeting_id_to_file_uuid,
        "file_uuid_to_meeting_id": file_uuid_to_meeting_id,
    }

    client = get_opensearch_client()
    if client is not None:
        client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    indexer = TranscriptIndexingService()
    db = SessionLocal()
    try:
        for record in records.values():
            media_file = db.get(MediaFile, record.media_file_id)
            if media_file is not None:
                db.query(TranscriptSegment).filter(
                    TranscriptSegment.media_file_id == media_file.id
                ).delete()
                db.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete()
                db.delete(media_file)
                db.commit()
            indexer.delete_transcript_chunks(record.file_uuid)
    finally:
        db.close()


@pytest.fixture(scope="session")
def neural_available(search_corpus_token):
    """Skip the calling (semantic) test rather than fail if no neural model is deployed."""
    headers = {"Authorization": f"Bearer {search_corpus_token}"}
    resp = requests.get(f"{BASE}/search/models/neural", headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("neural_enabled"):
        pytest.skip("Neural search disabled in this environment")
    active = [m for m in data.get("models", []) if m.get("model_id") == data.get("active_model_id")]
    state = (active[0].get("model_state") or active[0].get("state")) if active else None
    if state != "DEPLOYED":
        pytest.skip(f"Neural model not deployed (state={state!r}) — semantic search unavailable")
    return True
