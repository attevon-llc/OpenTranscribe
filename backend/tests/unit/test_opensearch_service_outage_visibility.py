"""Outage visibility for the ``opensearch_service`` handlers (broad-except lane B9).

Three contracts, each asserted against a client whose every call fails:

* a writer/updater reports the failure through its return value AND logs a traceback;
* an error that is not an OpenSearch error (a bug) propagates instead of being flattened
  into the same "failed" value;
* a reader taking ``strict=True`` raises ``OpenSearchUnavailableError`` so "nothing found"
  and "could not look" are different answers.
"""

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from opensearchpy.exceptions import NotFoundError

from app.services.opensearch_service import clusters
from app.services.opensearch_service import matching
from app.services.opensearch_service import profiles
from app.services.opensearch_service import speaker_collections
from app.services.opensearch_service import speaker_maintenance
from app.services.opensearch_service import speaker_metadata
from app.services.opensearch_service import speaker_read
from app.services.opensearch_service import transcripts
from app.services.opensearch_service.client import OpenSearchUnavailableError

_CLIENT_PATH = "app.services.opensearch_service.client.opensearch_client"
_METHODS = ("index", "update", "delete", "bulk", "search", "get", "mget", "msearch", "count")


def _outage() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "cluster down", Exception())


def _failing_client(exc: Exception) -> MagicMock:
    client = MagicMock()
    for name in _METHODS:
        getattr(client, name).side_effect = exc
    return client


def _neutralise_bootstrap(stack_modules: tuple[Any, ...]) -> list[Any]:
    patches = []
    for mod in stack_modules:
        for attr, value in (
            ("ensure_indices_exist", MagicMock()),
            ("get_active_speaker_index", MagicMock(return_value="idx")),
            ("assert_voiceprint_write_allowed", MagicMock(return_value=True)),
            ("get_speaker_index", MagicMock(return_value="idx")),
            ("query_embedding_dimension_mismatch", MagicMock(return_value=False)),
        ):
            if hasattr(mod, attr):
                patches.append(patch.object(mod, attr, value))
    return patches


def _call_with(exc: Exception, module: Any, fn: Callable[[], Any]) -> Any:
    patches = _neutralise_bootstrap((module,))
    for p in patches:
        p.start()
    try:
        with patch(_CLIENT_PATH, _failing_client(exc)):
            return fn()
    finally:
        for p in patches:
            p.stop()


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.exc_info]


_EMB = [0.1] * 256

SOFT_WRITERS: list[tuple[str, Any, Callable[[], Any], Any]] = [
    (
        "store_cluster_embedding",
        clusters,
        lambda: clusters.store_cluster_embedding("c", 1, _EMB),
        False,
    ),
    ("delete_cluster_embedding", clusters, lambda: clusters.delete_cluster_embedding("c"), False),
    (
        "update_cluster_embedding",
        clusters,
        lambda: clusters.update_cluster_embedding("c", _EMB),
        False,
    ),
    (
        "update_speaker_collections",
        speaker_collections,
        lambda: speaker_collections.update_speaker_collections("s", 1, "p", [1]),
        None,
    ),
    (
        "move_speaker_to_profile_collection",
        speaker_collections,
        lambda: speaker_collections.move_speaker_to_profile_collection("s", 1, "p", [1]),
        None,
    ),
    (
        "bulk_update_collection_assignments",
        speaker_collections,
        lambda: speaker_collections.bulk_update_collection_assignments(
            [{"speaker_uuid": "s", "profile_id": 1, "profile_uuid": "p", "collection_ids": [1]}]
        ),
        None,
    ),
    (
        "merge_speaker_embeddings",
        speaker_maintenance,
        lambda: speaker_maintenance.merge_speaker_embeddings("a", "b", [1]),
        None,
    ),
    (
        "update_speaker_segment_count",
        speaker_metadata,
        lambda: speaker_metadata.update_speaker_segment_count("s", 3),
        False,
    ),
    (
        "update_speaker_display_name",
        speaker_metadata,
        lambda: speaker_metadata.update_speaker_display_name("s", "Ann"),
        None,
    ),
    (
        "update_speaker_profile",
        speaker_metadata,
        lambda: speaker_metadata.update_speaker_profile("s", 1, "p"),
        None,
    ),
    (
        "store_profile_embedding",
        profiles,
        lambda: profiles.store_profile_embedding(1, "p", "Ann", _EMB, 1, 1),
        False,
    ),
    (
        "store_profile_embedding_v4",
        profiles,
        lambda: profiles.store_profile_embedding_v4(1, "p", "Ann", _EMB, 1, 1),
        False,
    ),
    (
        "index_transcript",
        transcripts,
        lambda: transcripts.index_transcript(1, "f", 1, "text", ["Ann"], "t", organization_id=None),
        None,
    ),
    (
        "update_transcript_title",
        transcripts,
        lambda: transcripts.update_transcript_title("f", "new"),
        None,
    ),
]
_IDS = [c[0] for c in SOFT_WRITERS]


@pytest.mark.parametrize(("name", "module", "call", "failed"), SOFT_WRITERS, ids=_IDS)
def test_writer_reports_outage_with_traceback(name, module, call, failed, caplog):
    with caplog.at_level(logging.DEBUG):
        result = _call_with(_outage(), module, call)
    assert result == failed
    assert _tracebacks(caplog), f"{name} swallowed the outage without a traceback"


@pytest.mark.parametrize(("name", "module", "call", "failed"), SOFT_WRITERS, ids=_IDS)
def test_writer_does_not_swallow_a_programming_error(name, module, call, failed):
    with pytest.raises(RuntimeError, match="bug"):
        _call_with(RuntimeError("bug"), module, call)


STRICT_READERS: list[tuple[str, Any, Callable[[bool], Any], Any]] = [
    (
        "get_profile_embedding",
        profiles,
        lambda s: profiles.get_profile_embedding("p", strict=s),
        None,
    ),
    (
        "find_matching_profiles",
        profiles,
        lambda s: profiles.find_matching_profiles(_EMB, 1, strict=s),
        [],
    ),
    (
        "msearch_profile_knn_batch",
        profiles,
        lambda s: profiles.msearch_profile_knn_batch({"u": _EMB}, 1, strict=s),
        {"u": []},
    ),
    (
        "get_speaker_document",
        speaker_read,
        lambda s: speaker_read.get_speaker_document("s", strict=s),
        None,
    ),
    (
        "get_speaker_embeddings_batch",
        speaker_read,
        lambda s: speaker_read.get_speaker_embeddings_batch(["s"], strict=s),
        {},
    ),
    (
        "get_all_speaker_embeddings",
        speaker_read,
        lambda s: speaker_read.get_all_speaker_embeddings(1, strict=s),
        [],
    ),
    (
        "msearch_speaker_similarities",
        matching,
        lambda s: matching.msearch_speaker_similarities(
            [{"speaker_uuid": "s", "embedding": _EMB}], 1, strict=s
        ),
        [[]],
    ),
]
_READ_IDS = [c[0] for c in STRICT_READERS]


@pytest.mark.parametrize(("name", "module", "call", "empty"), STRICT_READERS, ids=_READ_IDS)
def test_reader_strict_raises_on_outage(name, module, call, empty):
    with pytest.raises(OpenSearchUnavailableError):
        _call_with(_outage(), module, lambda: call(True))


@pytest.mark.parametrize(("name", "module", "call", "empty"), STRICT_READERS, ids=_READ_IDS)
def test_reader_default_stays_soft_but_logs_traceback(name, module, call, empty, caplog):
    with caplog.at_level(logging.DEBUG):
        result = _call_with(_outage(), module, lambda: call(False))
    assert result == empty
    assert _tracebacks(caplog)


def test_missing_profile_is_not_an_outage(caplog):
    exc = NotFoundError(404, "not_found", {})
    with caplog.at_level(logging.DEBUG):
        result = _call_with(exc, profiles, lambda: profiles.get_profile_embedding("p", strict=True))
    assert result is None
    assert not _tracebacks(caplog)
