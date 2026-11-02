"""``takedown_predicates.quarantined_among`` and the four post-filters built on it.

The search results page, chat's ranked evidence, chat's counted aggregation shapes and the
replay of persisted chat citations all drop index-backed hits whose file is quarantined, and
all four ask the same question through this one predicate. The Postgres cases pin the
predicate itself; the session-stub cases pin, per caller, the three properties that each of
the four hand-written queries had before they were shared:

* a quarantined uuid the predicate reports is dropped by every caller;
* an empty input (or one with no usable uuid) runs no query at all;
* a query error propagates. Each caller drops hits on the strength of this answer, so an
  error that read as "nothing quarantined" would pass a taken-down file through.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.endpoints.chat.citation_takedown import drop_quarantined_citations
from app.api.endpoints.chat.citation_takedown import drop_quarantined_citations_bulk
from app.api.endpoints.search_helpers import _drop_quarantined_search_hits
from app.services.chat.aggregation_queries import _quarantined_among
from app.services.chat.evidence import _drop_quarantined_hits
from app.services.search.chunk_retrieval import ChunkHit
from app.services.takedown_predicates import quarantined_among

pytestmark = pytest.mark.unit

BLOCKED = str(uuid_pkg.uuid4())
VISIBLE = str(uuid_pkg.uuid4())


def _make_file(db, user, *, quarantined: bool):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="quarantined-among.mp4",
        title="Recording",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


# ---------------------------------------------------------------------------
# The predicate, against Postgres.
# ---------------------------------------------------------------------------


def test_returns_only_the_quarantined_uuids_as_strings(db_session, normal_user):
    blocked = _make_file(db_session, normal_user, quarantined=True)
    visible = _make_file(db_session, normal_user, quarantined=False)

    found = quarantined_among(db_session, [str(blocked.uuid), str(visible.uuid)])

    assert found == {str(blocked.uuid)}


def test_accepts_uuid_objects_and_skips_falsy_ids(db_session, normal_user):
    blocked = _make_file(db_session, normal_user, quarantined=True)

    found = quarantined_among(db_session, [blocked.uuid, None, ""])

    assert found == {str(blocked.uuid)}


def test_an_unknown_uuid_is_not_reported(db_session):
    assert quarantined_among(db_session, [str(uuid_pkg.uuid4())]) == set()


# ---------------------------------------------------------------------------
# The four callers, against a session stub.
# ---------------------------------------------------------------------------


def _session(quarantined: set[str] | None = None, error: Exception | None = None) -> MagicMock:
    """A session answering the predicate's one query with ``quarantined``, or raising ``error``."""
    session = MagicMock(spec=Session)
    if error is not None:
        session.query.side_effect = error
    rows = [(uuid,) for uuid in sorted(quarantined or set())]
    session.query.return_value.filter.return_value.all.return_value = rows
    return session


def _factory(session: MagicMock):
    @contextmanager
    def _open():
        yield session

    return _open


def _chunk(file_uuid: str) -> ChunkHit:
    return ChunkHit(file_uuid=file_uuid, file_id=0, chunk_index=0, content="c", title="t")


def _search_response(*file_uuids: str | None) -> SimpleNamespace:
    hits = [SimpleNamespace(file_uuid=uuid) for uuid in file_uuids]
    return SimpleNamespace(results=hits, total_results=len(hits), total_files=len(hits))


def _citation(file_uuid: str | None) -> dict[str, Any]:
    return {"file_uuid": file_uuid, "title": "t", "snippet": "s"}


def _kept_by_every_caller(session: MagicMock, *file_uuids: str) -> dict[str, list[str | None]]:
    """What each caller keeps of ``file_uuids``."""
    evidence = _drop_quarantined_hits(session, [_chunk(u) for u in file_uuids])
    search = _drop_quarantined_search_hits(session, _search_response(*file_uuids))
    citations = drop_quarantined_citations(
        session, [_citation(u) for u in file_uuids], is_admin=False
    )
    bulk = drop_quarantined_citations_bulk(
        session, [[_citation(u) for u in file_uuids]], is_admin=False
    )
    dropped = _quarantined_among(_factory(session), file_uuids)
    return {
        "evidence": [hit.file_uuid for hit in evidence],
        "search": [hit.file_uuid for hit in search.results],
        "citations": [c["file_uuid"] for c in citations],
        "citations_bulk": [c["file_uuid"] for c in bulk[0]],
        "aggregation": [u for u in file_uuids if u not in dropped],
    }


def test_every_caller_drops_what_the_predicate_reports():
    kept = _kept_by_every_caller(_session(quarantined={BLOCKED}), BLOCKED, VISIBLE)

    assert kept == dict.fromkeys(kept, [VISIBLE])
    assert len(kept) == 5


def test_search_totals_drop_with_the_hits():
    response = _drop_quarantined_search_hits(
        _session(quarantined={BLOCKED}), _search_response(BLOCKED, VISIBLE)
    )

    assert (response.total_results, response.total_files) == (1, 1)


@pytest.mark.parametrize("file_uuids", [(), (None,), ("",)])
def test_no_caller_queries_for_an_input_without_uuids(file_uuids):
    session = _session(error=AssertionError("queried for an input with no uuids"))

    assert _drop_quarantined_hits(session, [_chunk(u) for u in file_uuids if u is not None]) == [
        _chunk(u) for u in file_uuids if u is not None
    ]
    response = _search_response(*file_uuids)
    assert _drop_quarantined_search_hits(session, response) is response
    citations = [_citation(u) for u in file_uuids]
    assert drop_quarantined_citations(session, citations, is_admin=False) is citations
    assert drop_quarantined_citations_bulk(session, [citations], is_admin=False) == [citations]
    assert _quarantined_among(_factory(session), file_uuids) == frozenset()
    assert quarantined_among(session, file_uuids) == set()
    assert session.query.call_count == 0


def _broken() -> MagicMock:
    return _session(error=OperationalError("SELECT media_file.uuid", {}, Exception("db down")))


def test_a_query_error_fails_the_chat_evidence_drop():
    with pytest.raises(OperationalError):
        _drop_quarantined_hits(_broken(), [_chunk(BLOCKED)])


def test_a_query_error_fails_the_search_drop():
    with pytest.raises(OperationalError):
        _drop_quarantined_search_hits(_broken(), _search_response(BLOCKED))


def test_a_query_error_fails_the_citation_drop():
    with pytest.raises(OperationalError):
        drop_quarantined_citations(_broken(), [_citation(BLOCKED)], is_admin=False)
    with pytest.raises(OperationalError):
        drop_quarantined_citations_bulk(_broken(), [[_citation(BLOCKED)]], is_admin=False)


def test_a_query_error_fails_the_aggregation_post_filter():
    with pytest.raises(OperationalError):
        _quarantined_among(_factory(_broken()), [BLOCKED])


def test_aggregation_declines_without_a_session():
    assert _quarantined_among(None, [BLOCKED]) == frozenset()
