"""Session-scoped media the E2E suite OWNS, so no test reads the dev library.

**The rule this module exists to enforce.** A test that asserts on whatever
recording happens to sit in a developer's dev deployment is unfailable for one
developer and unpassable for the next, and — worse — *silently skips* when the
library is empty. Nine E2E files did exactly that: they listed ``/api/files``,
picked something ``completed``, and skipped with messages like ``"No completed
file in dev dataset"``. On a ``--fresh`` stack the entire transcript / search /
chat surface therefore skipped while the gate still printed PASS. See
``backend/tests/CLAUDE.md``, "A gate test may not depend on the dev deployment's
DATA".

**Two owned artifacts, because they answer different questions.**

``owned_transcribed_file`` is a REAL upload of the committed 10 s clip put
through the REAL pipeline — ASR, diarization, MinIO object, OpenSearch chunks.
It is what "test the full system end to end" means, and it is the only one of
the two that can back a *search* assertion (nothing else puts chunks in the
index) or a *player* assertion (nothing else puts an object in storage).
Measured on this host, 2026-09-07: **37.9 s** from POST to a stably-completed,
redaction-settled file. Session-scoped, so the whole suite pays that once.

``owned_diarized_transcript_file`` and ``owned_long_transcript_file`` are
INJECTED — real ``MediaFile`` / ``Speaker`` / ``TranscriptSegment`` rows written
by the production corpus-injection tool (``app.scripts.corpus_injection``), no
ASR. They exist because two things a real 10 s clip cannot supply are
load-bearing for the file-detail suite: **more than one speaker** (the clip is
single-voice narration, so ``Edit Speakers`` has nothing to edit) and **more
than 500 segments** (the transcript-pagination invariants). They are separate
fixtures rather than one 560-segment file used for everything because
``detail_page`` is re-created per test: making every one of those ten setups
render 500 rows would buy nothing the twelve-segment file does not already
prove. Measured: **~2 s each**. Neither carries a media object, and neither is
indexed into OpenSearch — see :func:`_inject_transcript`.

All three are deleted in a fixture teardown that runs however the test ends —
``finally``, not the happy path, because the assertion most likely to fail is the
one after the object was created. All three carry ``conftest.OWNED_MEDIA_PREFIX``
in their filename, in the exact shape ``scripts/cleanup-test-data.py``'s ``owned``
spec matches, so a run killed before its teardown is still swept.

⚠️ **No redaction test may be built on the injected fixtures** — see the note in
:func:`_inject_transcript` about ``redaction_status``. ``test_redaction_e2e.py``
uses ``owned_transcribed_file``, which goes through the real scan.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from conftest import COMMITTED_SAMPLE_MEDIA
from conftest import OWNED_MEDIA_PREFIX
from conftest import delete_media_file
from conftest import wait_for_stable_completion

#: One page of ``GET /files/{uuid}/segments``, and the boundary the transcript
#: pagination invariants are about.
SEGMENT_PAGE_SIZE = 500

#: How many turns the injected long transcript carries.
#:
#: ``test_file_detail_transcript.py::test_scrolling_loads_and_renders_every_segment``
#: asserts ``len(ids) > 500``, so the transcript has to cross that boundary for the
#: infinite-scroll sentinel to be exercised at all. 560 leaves a full page plus a
#: partial one, which is the case that found the duplicate-``overlap_group_id``
#: render crash; a value of exactly 501 would page once and prove less.
LONG_TRANSCRIPT_TURNS = 560

#: How many turns the injected diarized transcript carries. Small on purpose — see
#: ``owned_diarized_transcript_file``. Twelve gives the find bar several segments to
#: navigate between while still painting in a single request.
DIARIZED_TRANSCRIPT_TURNS = 12

#: Distinct speaker labels on the injected transcript. Two, not one: the
#: ``Edit Speakers`` affordance and the speaker-editor tests need a diarized file,
#: and the committed 10 s clip is single-voice narration.
LONG_TRANSCRIPT_SPEAKERS = ("Ada Vance", "Bo Ruiz")

#: Shortest token :func:`_derive_search_term` will accept out of a transcript.
#: Four-letter words in this clip ("cold", "each") are common enough across a real
#: dev library to make a result assertion ambiguous about *which* file matched.
_MIN_SEARCH_TERM_LEN = 5


def _derive_search_term(segments: list[dict[str, Any]]) -> str:
    """Pick a query term that is provably present in *this* transcript.

    Derived, never hard-coded. ``test_search.py`` used to carry
    ``KNOWN_QUERY = "PyTorch"`` — "a term present in the standard dev corpus" — and
    every result-dependent test in the file skipped when it was not. Replacing one
    literal with another literal would only move the problem, so this reads the
    longest alphabetic token out of the transcript the fixture just created.

    Longest rather than first because length is a cheap proxy for distinctiveness:
    the search index is shared with the developer's own library, and a short common
    word makes "did the search return results" true for reasons unrelated to the
    owned file.

    Args:
        segments: ``transcript_segments`` from ``GET /api/files/{uuid}``.

    Returns:
        A lower-cased term of at least :data:`_MIN_SEARCH_TERM_LEN` letters.

    Raises:
        AssertionError: if the transcript yields no usable token. That is a real
            failure of the seeded fixture, not a reason to skip — a search test
            with no term to search for proves nothing.
    """
    candidates: set[str] = set()
    for segment in segments:
        for raw in (segment.get("text") or "").split():
            cleaned = "".join(ch for ch in raw if ch.isalpha())
            if len(cleaned) >= _MIN_SEARCH_TERM_LEN:
                candidates.add(cleaned.lower())
    assert candidates, (
        "the owned transcript yielded no token of "
        f"{_MIN_SEARCH_TERM_LEN}+ letters to search for; segments={segments!r:.400}"
    )
    return max(sorted(candidates), key=len)


def _wait_for_chunks_indexed(file_uuid: str, timeout: float = 120.0) -> int:
    """Poll OpenSearch directly until *file_uuid* has transcript chunks.

    Deliberately NOT ``GET /api/search``: the app caches search responses for
    ``SEARCH_CACHE_TTL_SECONDS`` (300 s) keyed on query/user/page, so polling it
    would pin a "nothing indexed yet" answer for five minutes — well past this
    timeout. Same reasoning, and the same shape, as
    ``tests/fixtures/search_corpus_stack.py::_wait_for_indexed``.

    Returns:
        The chunk count once it is non-zero.

    Raises:
        AssertionError: on timeout, or when no OpenSearch client is configured.
            A search test running against an unindexed file would fail with a
            message about the UI; failing here names the real cause.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import chunk_plane_query

    client = get_opensearch_client()
    assert client is not None, (
        "OpenSearch client unavailable — the owned file cannot be verified as searchable"
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
        count = int(
            client.count(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={"query": chunk_plane_query(file_uuid)},
            ).get("count", 0)
        )
        if count > 0:
            return count
        # Pure index polling — no Playwright page exists here, so there is no
        # locator to auto-wait on and the sleep IS the poll interval (issue #431).
        time.sleep(2.0)
    raise AssertionError(
        f"owned file {file_uuid} never got transcript chunks indexed within {timeout}s. "
        "Search assertions against it would be measuring an empty index."
    )


@pytest.fixture(scope="session")
def owned_transcribed_file(backend_url: str, admin_token: str) -> Iterator[dict[str, Any]]:
    """A REAL upload of the committed clip, fully processed, deleted on teardown.

    Session-scoped on purpose. A real transcription costs ~38 s (measured, see the
    module docstring); paying that per module — let alone per test — would add many
    minutes to a suite whose whole job is to be runnable during development. Nothing
    any consumer does mutates it: every test that writes (a rename, a reprocess) already
    uses ``owned_media_factory`` for its own throwaway copy.

    Yields:
        ``{"uuid", "detail", "segments", "search_term", "indexed_chunks"}`` —
        ``detail`` is the full ``GET /api/files/{uuid}`` payload, ``search_term``
        a token :func:`_derive_search_term` proved is in the transcript.
    """
    assert COMMITTED_SAMPLE_MEDIA.exists(), (
        f"missing committed media fixture {COMMITTED_SAMPLE_MEDIA} — "
        "see backend/tests/fixtures/media/README.md"
    )

    headers = {"Authorization": f"Bearer {admin_token}"}
    name = f"{OWNED_MEDIA_PREFIX}{uuid.uuid4().hex[:8]}{COMMITTED_SAMPLE_MEDIA.suffix}"
    with COMMITTED_SAMPLE_MEDIA.open("rb") as handle:
        response = requests.post(
            f"{backend_url}/api/files",
            headers=headers,
            files={"file": (name, handle, "audio/wav")},
            timeout=300,
        )
    assert response.status_code == 200, (
        f"owned_transcribed_file upload failed: {response.status_code} {response.text[:300]}"
    )
    file_uuid = str(response.json()["uuid"])

    try:
        status = wait_for_stable_completion(backend_url, admin_token, file_uuid, timeout_secs=600)
        assert status == "completed", (
            f"the owned upload never completed (status={status}). Every transcript, search "
            "and chat assertion in this suite is built on it, so this is a hard failure "
            "rather than a skip."
        )

        detail_response = requests.get(
            f"{backend_url}/api/files/{file_uuid}", headers=headers, timeout=30
        )
        assert detail_response.status_code == 200, (
            f"could not re-read the owned upload: {detail_response.text[:300]}"
        )
        detail = dict(detail_response.json())
        segments = list(detail.get("transcript_segments") or [])
        assert segments, "the owned upload completed with no transcript segments"

        chunks = _wait_for_chunks_indexed(file_uuid)

        yield {
            "uuid": file_uuid,
            "detail": detail,
            "segments": segments,
            "search_term": _derive_search_term(segments),
            "indexed_chunks": chunks,
        }
    finally:
        delete_media_file(backend_url, admin_token, file_uuid)


@pytest.fixture(scope="session")
def owned_search_term(owned_transcribed_file: dict[str, Any]) -> str:
    """A query term proved present in the owned transcript AND in the search index."""
    return str(owned_transcribed_file["search_term"])


def _transcript_turns(count: int) -> list[Any]:
    """Build *count* alternating-speaker turns.

    Each line embeds its own index, which is not cosmetic: the live DDL carries
    ``UNIQUE (media_file_id, start_time, end_time, md5(text))``
    (``uq_transcript_segment_content``), so a transcript of identical lines would
    abort the bulk insert. Timings are left ``None`` and generated by
    ``resolve_timings``' synthetic path, which records the provenance as
    ``synthetic`` on the row — nothing may read these as measurements.
    """
    from app.scripts.corpus_injection.model import Turn

    topics = (
        "the retention policy for archived recordings",
        "how the ingest queue behaves under backpressure",
        "the migration window for the search index",
        "which alerts should page someone overnight",
        "the rollout plan for the new transcript viewer",
    )
    return [
        Turn(
            turn_index=index,
            speaker=LONG_TRANSCRIPT_SPEAKERS[index % len(LONG_TRANSCRIPT_SPEAKERS)],
            text=(
                f"Point {index + 1}: let us settle {topics[index % len(topics)]} "
                "before the review closes, and record the decision against this agenda item."
            ),
        )
        for index in range(count)
    ]


def _admin_user_id(email: str) -> int:
    """The integer PK of the shared E2E admin account.

    The injection tool writes rows directly, so it needs the FK value the API never
    exposes (``/api/users/me`` returns the public ``uuid``). Read through the app's
    own ``SessionLocal``, which ``tests/conftest.py``'s module-level side effects
    have already pointed at the dev stack's Postgres — see ``e2e/conftest.py``'s
    import of it.
    """
    from app.db.base import SessionLocal
    from app.models.user import User

    session = SessionLocal()
    try:
        user_id = session.query(User.id).filter(User.email == email).scalar()
        assert user_id is not None, f"the E2E admin account {email} does not exist"
        return int(user_id)
    finally:
        session.close()


def _inject_transcript(
    backend_url: str, admin_token: str, *, meeting_id: str, title: str, turns: int
) -> Iterator[dict[str, Any]]:
    """Inject one owned transcript, yield a descriptor, delete it however we exit.

    Created through ``app.scripts.corpus_injection`` — the same tool
    ``tests/fixtures/search_corpus_stack.py`` uses — so the rows are the ones the
    transcription pipeline itself writes. ASR is the only stage skipped, and that is
    the point: these fixtures back the file-detail surface, which reads Postgres and
    never touches the audio.

    ``dispatch_indexing`` is deliberately **not** called. Nothing that consumes these
    searches; indexing 560 segments of filler would push it into the developer's
    shared OpenSearch index for the duration of the run, where it would compete with
    their real recordings in their own searches. Search assertions belong on
    ``owned_transcribed_file``, which is indexed for real.
    """
    from conftest import TEST_ADMIN_EMAIL

    from app.core.constants import REDACTION_STATUS_DONE
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.scripts.corpus_injection.injector import inject_meeting
    from app.scripts.corpus_injection.model import MeetingDoc

    user_id = _admin_user_id(TEST_ADMIN_EMAIL)
    # uuid4 in the seed: `file_uuid` is a pure function of (corpus, meeting_id, seed),
    # so a fixed seed would make two concurrent runs collide on one row — and the
    # loser's teardown would delete the winner's file mid-test.
    seed = f"e2e-owned-{uuid.uuid4().hex[:10]}"
    doc = MeetingDoc(
        corpus="e2e_owned_corpus",
        meeting_id=meeting_id,
        title=title,
        turns=_transcript_turns(turns),
        language="en",
    )

    session = SessionLocal()
    try:
        record, _ = inject_meeting(session, doc, user_id, seed=seed, tool_version="e2e-fixture")
        # Rename to carry OWNED_MEDIA_PREFIX in the exact shape
        # `scripts/cleanup-test-data.py`'s `owned` spec matches, so a run killed
        # before this fixture's teardown still gets swept. The injector's own
        # `{corpus}__{meeting_id}.transcript` name matches nothing there.
        media_file = session.get(MediaFile, record.media_file_id)
        assert media_file is not None, "inject_meeting returned an id that does not resolve"
        media_file.filename = f"{OWNED_MEDIA_PREFIX}{uuid.uuid4().hex[:8]}.transcript"

        # ⚠️ Without this the transcript is INVISIBLE through the API, and the
        # reason is not obvious from either side.
        #
        # `inject_meeting` leaves `redaction_status` NULL — the transcription
        # pipeline sets it, and injection skips the pipeline. But
        # `api/endpoints/files/crud.py::_withheld_for_redaction` treats
        # `None | pending | processing` alike, so `GET /api/files/{uuid}` and
        # `/segments` both answer `transcript_segments: []` with
        # `redaction_pending: true` **forever**. Measured directly: 12 rows in
        # `transcript_segment`, 0 through the API. That is a live defect in the
        # injector itself, not something this fixture introduced — every corpus
        # `app/scripts/corpus_injection` has ever written has an unreadable
        # transcript in the product, which the RAG eval harness never noticed
        # because it calls `retrieve_chunks` in-process and never touches the
        # HTTP file surface.
        #
        # The honest repair here would be to dispatch the real `redaction.detect`
        # task, but the E2E process has no broker: `tests/conftest.py` sets
        # `SKIP_REDIS=True` and Celery would publish to localhost:6379, which on
        # this host is an unrelated container (see that file's own note). So the
        # fixture writes the terminal state the scan would reach. It is accurate
        # in substance — this transcript is generated filler containing no PII,
        # so "scanned, nothing to mask" is the correct end state — but it is
        # asserted here rather than measured, which is why no redaction test may
        # be built on this fixture. `test_redaction_e2e.py` uses
        # `owned_transcribed_file`, which goes through the real scan.
        media_file.redaction_status = REDACTION_STATUS_DONE
        session.commit()
    finally:
        session.close()

    assert record.segment_count == turns, (
        f"injected {record.segment_count} segments, expected {turns} — the fixture's "
        "own precondition, not something a consuming test should discover"
    )

    # Prove the rows are readable THROUGH THE API before any test acts on them. Rows in
    # `transcript_segment` are not the same claim as a transcript the product will serve
    # — the redaction gate above is exactly that gap, and it presented as ten unrelated
    # setup errors in files that had nothing to do with redaction.
    served = requests.get(
        f"{backend_url}/api/files/{record.file_uuid}/segments",
        headers={"Authorization": f"Bearer {admin_token}"},
        params={"segment_limit": "1"},
        timeout=30,
    )
    assert served.status_code == 200, (
        f"owned injected transcript unreadable: {served.status_code} {served.text[:300]}"
    )
    served_total = served.json().get("total_segments", 0)
    assert served_total == turns, (
        f"the API serves {served_total} of the {turns} injected segments "
        f"(redaction_pending={served.json().get('redaction_pending')!r}). The rows exist; "
        "something is withholding them."
    )

    try:
        yield {
            "uuid": record.file_uuid,
            "media_file_id": record.media_file_id,
            "segment_count": record.segment_count,
            "speakers": list(LONG_TRANSCRIPT_SPEAKERS),
            "title": title,
        }
    finally:
        # The product's own delete path, not a hand-rolled row sweep: it removes the
        # segments, the speakers, the storage object (there is none — the endpoint
        # tolerates that) and the OpenSearch chunks in one call, and it is the same
        # cleanup every other owned artifact in this suite uses.
        delete_media_file(backend_url, admin_token, record.file_uuid)


@pytest.fixture(scope="session")
def owned_diarized_transcript_file(backend_url: str, admin_token: str) -> Iterator[dict[str, Any]]:
    """An INJECTED two-speaker transcript, short enough to re-render per test.

    :data:`DIARIZED_TRANSCRIPT_TURNS` segments across
    :data:`LONG_TRANSCRIPT_SPEAKERS` — enough for the transcript region, the export
    control, the in-transcript find bar and the ``Edit Speakers`` editor, and small
    enough that the file-detail page paints in one request.
    """
    yield from _inject_transcript(
        backend_url,
        admin_token,
        meeting_id="diarized-transcript",
        title="E2E owned diarized transcript",
        turns=DIARIZED_TRANSCRIPT_TURNS,
    )


@pytest.fixture(scope="session")
def owned_long_transcript_file(backend_url: str, admin_token: str) -> Iterator[dict[str, Any]]:
    """An INJECTED transcript longer than one :data:`SEGMENT_PAGE_SIZE` page."""
    descriptor_source = _inject_transcript(
        backend_url,
        admin_token,
        meeting_id="long-transcript",
        title="E2E owned long transcript",
        turns=LONG_TRANSCRIPT_TURNS,
    )
    descriptor = next(descriptor_source)
    assert descriptor["segment_count"] > SEGMENT_PAGE_SIZE, (
        f"the injected transcript has only {descriptor['segment_count']} segments; the "
        f"pagination invariants need more than one {SEGMENT_PAGE_SIZE}-segment page"
    )
    try:
        yield descriptor
    finally:
        # Drive the generator's own `finally` (the delete) exactly once.
        next(descriptor_source, None)
