"""The indexed speaker label must equal what the resolver derives from the database.

**This invariant exists because it found a real misattribution that nothing else could see.**

Measured on a dev index: 53 of 54 ``speaker_id`` values agreed with
``canonical_speaker_label()``. One did not — ``speaker_id=2800`` carried
``speaker="Joe Rogan"`` across **398 chunks**, while its ``Speaker`` row is
``name=SPEAKER_00``, no ``display_name``, and a *suggested* name of "Joe Rogan" at
confidence **0.7006** — below ``DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD`` (0.75). So the
resolver says that speaker is **unidentified**, and the search index says he is Joe Rogan.

Why that matters more than a stale string: chat cites chunks. Those 398 would have been
attributed to a named person on the strength of a sub-threshold guess the product's own
rule rejects — which also sits against the standing "LLM speaker suggestions are never
auto-applied" rule.

**The cause is a class, not an instance.** Chunks are labelled at INDEX time, so every
change to the labelling rule leaves already-indexed documents behind. ``canonical_speaker_label``
(issue W2.0b) unified a rule that four planes previously disagreed on; the documents written
under the old rule were never recomputed. The same shape produced the milder ``"Unknown"``
vs ``"Unknown Speaker"`` split. Any future change to the resolver reopens it.

⚠️ **A unit test cannot catch this.** Both sides are live state — the label is in OpenSearch,
the truth is in Postgres — so the only way to see the disagreement is to compare the two
systems. That is why this lives in ``tests/integration`` and drives a real cluster.

## Why this file no longer sweeps the shared dev index

It used to run the sweep below over whatever happened to be in the developer's live
``transcript_chunks``. That made the result a function of **the dev deployment's data**, which
is different for every developer, on every machine, on every day — so the test was unfailable
for one person and unpassable for the next.

It failed exactly that way on 2026-09-06: ``speaker_id=147487`` carried
``'E2E Propagated 270d60f9'`` in the index while its row resolved to a *different* e2e name.
Both values were **test residue** — an e2e run from 2026-08-27 had uploaded ``sample_short.wav``,
renamed its speaker, and never cleaned up. The rename dispatches its OpenSearch update to
Celery (``update_speaker`` → ``process_speaker_update_background.delay``), so the index is
eventually-consistent by design; the background task never landed and a stale label sat there
for ten days, failing a gate that had nothing to do with it. Chasing the *data* is unbounded:
the next run leaves new residue.

So the invariant is now asserted against **data this file creates and destroys**: a throwaway
chunks index (uuid4-named, real mapping, deleted in teardown) and speaker rows in the test
session's savepoint. Nothing here reads or writes the shared index, so the result depends only
on the code under test — and the negative control proves the sweep can still fail.

The whole-deployment sweep remains available as a deliberate **operational audit** of a real
index, behind ``RUN_INDEX_AUDIT``. It is not part of the gate, because auditing production data
and testing a code path are different jobs with different pass conditions.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.media import Speaker
from app.utils.speaker_labels import canonical_speaker_label

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). This compares two live systems; a "
            "stand-in index cannot show a disagreement between them."
        ),
    ),
]

_INDEX_SAMPLE_CAP = 1000


def _indexed_labels(client, index: str) -> dict[int, list[str]]:
    """``speaker_id`` -> the distinct ``speaker`` labels indexed against it."""
    resp = client.search(
        index=index,
        body={
            "size": 0,
            "query": {"bool": {"filter": [{"exists": {"field": "speaker_id"}}]}},
            "aggs": {
                "ids": {
                    "terms": {"field": "speaker_id", "size": _INDEX_SAMPLE_CAP},
                    "aggs": {"names": {"terms": {"field": "speaker", "size": 5}}},
                }
            },
        },
    )
    buckets = resp["aggregations"]["ids"]["buckets"]
    assert len(buckets) < _INDEX_SAMPLE_CAP, (
        f"hit the {_INDEX_SAMPLE_CAP} bucket cap — the sweep below would silently cover "
        "only part of the index. Raise the cap or paginate with a composite agg."
    )
    return {b["key"]: [n["key"] for n in b["names"]["buckets"]] for b in buckets}


def find_label_drift(client, index: str, db: Session) -> tuple[list[str], list[int]]:
    """Compare every indexed speaker label against the resolver. -> (drift, orphaned).

    The one implementation of the comparison, shared by the seeded tests and the opt-in
    audit — so the audit cannot drift from the rule the gate enforces.
    """
    drift: list[str] = []
    orphaned: list[int] = []
    for speaker_id, labels in _indexed_labels(client, index).items():
        row = db.query(Speaker).filter(Speaker.id == speaker_id).first()
        if row is None:
            orphaned.append(speaker_id)
            continue
        expected = canonical_speaker_label(
            row.name,
            display_name=row.display_name,
            suggested_name=row.suggested_name,
            confidence=row.confidence,
        )
        for label in labels:
            if label != expected:
                drift.append(
                    f"speaker_id={speaker_id}: index={label!r} but the DB row "
                    f"(name={row.name!r}, display={row.display_name!r}, "
                    f"suggested={row.suggested_name!r}@{row.confidence}) resolves to {expected!r}"
                )
    return drift, orphaned


@pytest.fixture
def isolated_chunk_index(monkeypatch):
    """A throwaway chunks index with the REAL mapping, deleted in teardown.

    The real mapping is load-bearing: ``speaker`` is a ``keyword`` and ``speaker_id`` must be
    aggregatable, which is what the sweep's terms aggregation depends on.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    # An assert, not a skip: the module-level `skipif` already established that a cluster is
    # reachable, so a None client here means the client factory is broken, not that OpenSearch
    # is absent. Skipping would hide that behind a green run — the silent-skip trap this repo
    # treats as false confidence. It also narrows the `Any | None` for mypy.
    assert client is not None, (
        "SKIP_OPENSEARCH reported a reachable cluster but get_opensearch_client() returned None"
    )

    name = f"test_label_drift_{uuid.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    try:
        yield client, name
    finally:
        client.indices.delete(index=name, ignore=[404])


@pytest.fixture
def seeded_speaker(db_session: Session):
    """A real ``Speaker`` row, created in the test session's savepoint.

    Rolled back with the session, so this leaves nothing behind in Postgres either — the
    OpenSearch half is handled by ``isolated_chunk_index``.
    """
    from app.core.enums import FileStatus
    from app.core.security import get_password_hash
    from app.models.media import MediaFile
    from app.models.user import User

    token = uuid.uuid4().hex[:8]
    user = User(
        email=f"labeldrift-{token}@example.invalid",
        full_name="Label Drift Fixture",
        hashed_password=get_password_hash("throwaway-password-1"),  # noqa: S106
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    media_file = MediaFile(
        user_id=user.id,
        filename=f"labeldrift-{token}.wav",
        storage_path=f"test/labeldrift/{token}.wav",
        content_type="audio/wav",
        file_size=1000,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    def _make(**speaker_kwargs: Any) -> Speaker:
        speaker = Speaker(
            media_file_id=media_file.id,
            user_id=user.id,
            name=speaker_kwargs.pop("name", "SPEAKER_00"),
            **speaker_kwargs,
        )
        db_session.add(speaker)
        db_session.commit()
        db_session.refresh(speaker)
        return speaker

    return _make, media_file


def _index_chunk(client, index: str, *, file_uuid: str, speaker_id: int, label: str) -> None:
    client.index(
        index=index,
        id=f"{file_uuid}_{speaker_id}",
        body={
            "file_uuid": file_uuid,
            "chunk_index": 0,
            "content": "seeded chunk for the label-drift invariant",
            "title": "label drift fixture",
            "speaker": label,
            "speaker_id": speaker_id,
            "speakers": [label],
            "tags": [],
            "content_type": "audio/wav",
            "accessible_user_ids": [1],
            "upload_time": "2026-09-06T00:00:00+00:00",
            "language": "en",
            "start_time": 0.0,
            "end_time": 9.0,
            "indexed_at": "2026-09-06T00:00:00+00:00",
        },
    )
    client.indices.refresh(index=index)


def test_a_label_matching_the_resolver_reports_no_drift(
    isolated_chunk_index, seeded_speaker, db_session: Session
):
    """The passing case. Without it, a sweep that silently matched nothing would look green."""
    client, index = isolated_chunk_index
    make_speaker, media_file = seeded_speaker

    speaker = make_speaker(display_name="Ada Lovelace")
    expected = canonical_speaker_label(
        speaker.name,
        display_name=speaker.display_name,
        suggested_name=speaker.suggested_name,
        confidence=speaker.confidence,
    )
    _index_chunk(
        client, index, file_uuid=str(media_file.uuid), speaker_id=speaker.id, label=expected
    )

    drift, orphaned = find_label_drift(client, index, db_session)

    assert drift == [], f"a label written by the resolver itself must not read as drift: {drift}"
    assert orphaned == [], f"the seeded speaker row exists, so nothing is orphaned: {orphaned}"


def test_a_label_disagreeing_with_the_resolver_is_detected(
    isolated_chunk_index, seeded_speaker, db_session: Session
):
    """The NEGATIVE CONTROL — the 398-chunk misattribution, reproduced deliberately.

    A sub-threshold ``suggested_name`` must resolve to *unidentified*, so an index that names
    the person is the exact defect this file exists to catch. Seeded rather than hunted for,
    which is what makes the detection reproducible instead of dependent on someone's dev data.
    """
    client, index = isolated_chunk_index
    make_speaker, media_file = seeded_speaker

    speaker = make_speaker(name="SPEAKER_00", suggested_name="Joe Rogan", confidence=0.7006)
    expected = canonical_speaker_label(
        speaker.name,
        display_name=speaker.display_name,
        suggested_name=speaker.suggested_name,
        confidence=speaker.confidence,
    )
    assert expected != "Joe Rogan", (
        "precondition: 0.7006 is below DEFAULT_SUGGESTION_CONFIDENCE_THRESHOLD, so the "
        f"resolver must NOT name this speaker — it returned {expected!r}. If this fires, the "
        "threshold moved and this control no longer reproduces the original defect."
    )

    _index_chunk(
        client, index, file_uuid=str(media_file.uuid), speaker_id=speaker.id, label="Joe Rogan"
    )

    drift, _ = find_label_drift(client, index, db_session)

    assert len(drift) == 1, f"the seeded disagreement must be reported exactly once: {drift}"
    assert f"speaker_id={speaker.id}" in drift[0]
    assert "'Joe Rogan'" in drift[0], "the report must quote the label the index actually holds"


def test_a_chunk_referencing_a_deleted_speaker_is_reported_as_orphaned(
    isolated_chunk_index, db_session: Session
):
    """The index must not silently reference speakers Postgres no longer has."""
    client, index = isolated_chunk_index
    missing_id = 2_000_000_000  # far beyond any real sequence value
    assert db_session.query(Speaker).filter(Speaker.id == missing_id).first() is None

    _index_chunk(client, index, file_uuid=str(uuid.uuid4()), speaker_id=missing_id, label="Ghost")

    drift, orphaned = find_label_drift(client, index, db_session)

    assert orphaned == [missing_id], f"expected exactly the orphan to be reported: {orphaned}"
    assert drift == [], "an orphan has no row to disagree with, so it is not drift"


@pytest.mark.skipif(
    os.environ.get("RUN_INDEX_AUDIT", "").lower() not in {"1", "true"},
    reason=(
        "Opt-in operational audit of the REAL index (RUN_INDEX_AUDIT=1). Deliberately out of "
        "the gate: its result depends on the deployment's accumulated data, not on the code "
        "under test, so it is a question about a deployment rather than a regression test."
    ),
)
def test_audit_the_real_index_for_label_drift(db_session: Session):
    """Sweep the live chunks index. Findings are REAL and want a reindex, not a code change."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    # Asserted rather than skipped: this audit is opt-in via RUN_INDEX_AUDIT, so the operator
    # explicitly asked for it. Answering "skipped" to that request looks like "audited, clean".
    assert client is not None, "RUN_INDEX_AUDIT was requested but no OpenSearch client exists"
    index = settings.OPENSEARCH_CHUNKS_INDEX
    if not client.indices.exists(index=index):
        pytest.skip("chunks index not present on this cluster")

    drift, orphaned = find_label_drift(client, index, db_session)

    assert not drift, (
        "the chunk index disagrees with canonical_speaker_label() — chunks are attributed "
        "to a speaker the resolver would name differently. Reindex the affected files; do "
        "NOT relax this assertion.\n  " + "\n  ".join(drift[:20])
    )
    assert not orphaned, (
        f"{len(orphaned)} speaker_id(s) in the index have no Speaker row: {orphaned[:10]}. "
        "The index is referencing deleted speakers."
    )
