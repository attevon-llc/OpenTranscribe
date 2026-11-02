"""``delete_transcript_chunks`` must say when it could not delete.

Its callers are file deletion / GDPR erasure (``opensearch_erasure._erase_transcript_chunks``)
and the full rebuild in ``reindex_transcript``. It used to return ``0`` for "nothing to
delete" and for "the cluster failed" alike, so a failed sweep read as a clean one: the
erasure could only notice through its survivor count, and a rebuild carried on as if the
file's old documents (digests included) were gone.

These tests drive the real method against a stand-in client. The controls
(``test_a_successful_delete_returns_the_count`` / ``test_an_absent_index_is_zero``) show the
legitimate "nothing there" answers are unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from opensearchpy.exceptions import NotFoundError

from app.core.exceptions import SearchIndexError
from app.services.opensearch_service.client import OpenSearchUnavailableError
from tests.fixtures.indexing_client import bind_indexing_client

FILE_UUID = "11111111-2222-3333-4444-555555555555"


class _Indices:
    def __init__(self, present: bool) -> None:
        self.present = present

    def exists(self, **_: Any) -> bool:
        return self.present

    def refresh(self, **_: Any) -> None:
        return None


class _Client:
    """Answers ``delete_by_query`` with a chosen response or exception; ``count`` with 0."""

    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        raises: Exception | None = None,
        present: bool = True,
    ) -> None:
        self.indices = _Indices(present)
        self._response = response if response is not None else {"deleted": 0}
        self._raises = raises
        self.delete_calls = 0

    def __bool__(self) -> bool:
        return True

    def delete_by_query(self, **_: Any) -> dict[str, Any]:
        self.delete_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._response

    def count(self, **_: Any) -> dict[str, int]:
        return {"count": 0}


def _unreachable() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "connection refused", Exception("refused"))


@pytest.fixture
def install(monkeypatch):
    def _install(client: _Client) -> _Client:
        bind_indexing_client(monkeypatch, client)
        monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", lambda: None)
        return client

    return _install


def _delete() -> int:
    from app.services.search.indexing_service import TranscriptIndexingService

    return TranscriptIndexingService().delete_transcript_chunks(FILE_UUID)


def test_an_unreachable_cluster_raises_instead_of_reporting_zero(install) -> None:
    install(_Client(raises=_unreachable()))

    with pytest.raises(OpenSearchUnavailableError):
        _delete()


def test_failures_reported_in_the_response_body_raise(install) -> None:
    """``delete_by_query`` reports per-document failures in the body, without raising."""
    install(_Client(response={"deleted": 3, "failures": [{"cause": "shard"}]}))

    with pytest.raises(SearchIndexError, match="1 failure"):
        _delete()


def test_a_successful_delete_returns_the_count(install) -> None:
    client = install(_Client(response={"deleted": 4, "failures": []}))

    assert _delete() == 4
    assert client.delete_calls == 1


def test_an_absent_index_is_zero(install) -> None:
    """The cluster answering "no such index" is a legitimate nothing-to-delete."""
    client = install(_Client(present=False))

    assert _delete() == 0
    assert client.delete_calls == 0


def test_an_index_removed_mid_call_is_zero(install) -> None:
    install(_Client(raises=NotFoundError(404, "index_not_found_exception", {})))

    assert _delete() == 0


def test_the_erasure_records_a_failed_delete_although_the_count_reads_zero(install) -> None:
    """The erasure must not audit as complete when the delete itself failed."""
    from app.services.opensearch_erasure import _erase_transcript_chunks

    install(_Client(raises=_unreachable()))
    residual: list[tuple[str, str]] = []

    _erase_transcript_chunks(FILE_UUID, lambda stage, err: residual.append((stage, str(err))))

    assert [stage for stage, _ in residual] == ["transcript_chunks"]
    assert "connection refused" in residual[0][1]


def test_the_rebuild_does_not_reindex_over_a_failed_delete(install, monkeypatch) -> None:
    """``reindex_transcript`` wants a clean slate; a failed delete must fail the rebuild."""
    from app.services.search.indexing_service import TranscriptIndexingService

    install(_Client(raises=_unreachable()))
    indexed: list[str] = []

    def _record_index(self: object, **kw: object) -> dict[str, int]:
        indexed.append(str(kw["file_uuid"]))
        return {"chunk_count": 1}

    monkeypatch.setattr(TranscriptIndexingService, "index_transcript_chunks", _record_index)

    with pytest.raises(OpenSearchUnavailableError):
        TranscriptIndexingService().reindex_transcript(
            file_id=1,
            file_uuid=FILE_UUID,
            user_id=1,
            segments=[],
            title="t",
            speakers=[],
            tags=[],
        )
    assert indexed == []
