"""W2.2: the speaker-focus PARALLEL retrieval leg (`retrieval.retrieve_context`'s
``speaker_focus_names``).

The core design constraint under test: this leg only ever WIDENS the candidate
pool a turn reranks over. It must never replace or narrow the main leg, and it
must never touch the explicit, hard ``speakers`` scope — those are two
different parameters threaded to two different `retrieve_chunks` calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.chat import retrieval
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


def _hit(file_uuid: str, index: int, score: float = 1.0) -> ChunkHit:
    return ChunkHit(
        file_uuid=file_uuid,
        file_id=abs(hash(file_uuid)) % 1000,
        chunk_index=index,
        content=f"{file_uuid} chunk {index}",
        title="Recording",
        speaker="Dana",
        start_time=float(index * 30),
        end_time=float(index * 30 + 30),
        score=score,
    )


def _no_op_context():
    """Patch cache lookups so every call falls through to a fresh retrieval."""
    return (
        patch("app.services.chat.retrieval_cache.get_cached", return_value=None),
        patch("app.services.chat.retrieval_cache.set_cached"),
    )


def test_speaker_focus_leg_is_not_run_when_no_names_given():
    with patch.object(retrieval, "retrieve_chunks", return_value=[_hit("f", 0)]) as mocked:
        p1, p2 = _no_op_context()
        with p1, p2:
            result = retrieval.retrieve_context(
                query="what did the team decide",
                user_id=1,
                organization_id=None,
                file_uuids=None,
                settings=ChatSettings(rerank_enabled=False),
            )
    mocked.assert_called_once()
    assert result.speaker_focus_added == 0


def test_speaker_focus_leg_widens_the_pool_and_dedupes_overlap():
    """Main leg finds 2 chunks in file A; the focus leg finds the SAME 2 plus
    one new chunk in file B — the merge must add exactly the one new hit."""
    main_hits = [_hit("file-a", 0), _hit("file-a", 1)]
    focus_hits = [_hit("file-a", 0), _hit("file-a", 1), _hit("file-b", 5)]

    with patch.object(retrieval, "retrieve_chunks", side_effect=[main_hits, focus_hits]) as mocked:
        p1, p2 = _no_op_context()
        with p1, p2:
            result = retrieval.retrieve_context(
                query="what did Dana say about pricing",
                user_id=1,
                organization_id=None,
                file_uuids=None,
                settings=ChatSettings(
                    rerank_enabled=False, final_chunks=10, max_chunks_per_file=10
                ),
                speaker_focus_names=["Dana"],
            )

    assert mocked.call_count == 2
    assert result.speaker_focus_added == 1
    got = {(c.file_uuid, c.chunk_index) for c in result.chunks}
    assert got == {("file-a", 0), ("file-a", 1), ("file-b", 5)}


def test_speaker_focus_leg_never_replaces_the_main_leg_when_focus_finds_nothing():
    main_hits = [_hit("file-a", 0)]
    with patch.object(retrieval, "retrieve_chunks", side_effect=[main_hits, []]) as mocked:
        p1, p2 = _no_op_context()
        with p1, p2:
            result = retrieval.retrieve_context(
                query="what did Dana say",
                user_id=1,
                organization_id=None,
                file_uuids=None,
                settings=ChatSettings(rerank_enabled=False),
                speaker_focus_names=["Dana"],
            )
    assert mocked.call_count == 2
    assert [(c.file_uuid, c.chunk_index) for c in result.chunks] == [("file-a", 0)]
    assert result.speaker_focus_added == 0


def test_speaker_focus_names_never_touch_the_explicit_hard_scope():
    """The main leg's `speakers=` kwarg is the EXPLICIT checkbox scope and must
    be exactly what the caller passed — never widened, narrowed, or replaced
    by `speaker_focus_names`, which drives only the SECOND call."""
    with patch.object(retrieval, "retrieve_chunks", return_value=[]) as mocked:
        p1, p2 = _no_op_context()
        with p1, p2:
            retrieval.retrieve_context(
                query="what did Dana say",
                user_id=1,
                organization_id=None,
                file_uuids=None,
                speakers=["Alice"],
                settings=ChatSettings(rerank_enabled=False),
                speaker_focus_names=["Dana"],
            )

    assert mocked.call_count == 2
    main_call, focus_call = mocked.call_args_list
    assert main_call.kwargs["speakers"] == ["Alice"]
    assert focus_call.kwargs["speakers"] == ["Dana"]


def test_speaker_focus_leg_is_skipped_on_an_exact_cache_hit():
    """A cache hit already reflects whatever leg mix produced it the first
    time this exact query/scope/settings ran — the focus leg only runs on a
    fresh retrieval, matching how the whole cached-retrieval unit is treated
    atomically elsewhere in this module."""
    cached_hits = [_hit("file-a", 0)]
    with (
        patch("app.services.chat.retrieval_cache.get_cached", return_value=cached_hits),
        patch.object(retrieval, "retrieve_chunks") as mocked,
    ):
        result = retrieval.retrieve_context(
            query="what did Dana say",
            user_id=1,
            organization_id=None,
            file_uuids=None,
            settings=ChatSettings(rerank_enabled=False),
            speaker_focus_names=["Dana"],
        )
    mocked.assert_not_called()
    assert result.cache_hit is True
    assert result.speaker_focus_added == 0
