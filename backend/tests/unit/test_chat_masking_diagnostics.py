"""``chunks_dropped_empty_after_masking`` — the last of Phase 0's meta counters.

Masking fails **closed**: a chunk whose redaction policy cannot be resolved has
its content replaced with ``""`` and is then filtered out before the prompt is
built. Without a counter, that is indistinguishable from retrieval simply
finding less material — a different defect, in a different subsystem, with a
different fix. Both readings end in "the model answered from less context than
you think", which is why #385's class of silent wrongness keeps recurring.
"""

from __future__ import annotations

from contextlib import contextmanager

from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit


def _hit(index: int) -> ChunkHit:
    return ChunkHit(
        file_uuid=f"11111111-1111-1111-1111-00000000000{index}",
        file_id=index,
        chunk_index=index,
        content=f"content {index}",
        title="Recording",
        speaker="Dana",
        start_time=float(index),
        end_time=float(index) + 5.0,
    )


@contextmanager
def _null_session():
    """`_prepare_context` opens its own short session for the masking phase; this
    keeps the module free of Postgres, since `mask_chunks` is stubbed below."""
    yield None


def _prepare(monkeypatch, *, masked: list[MaskedChunk], retrieved: int) -> dict:
    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_: RetrievalResult(chunks=[chunk.source for chunk in masked], retrieved=retrieved),
    )
    monkeypatch.setattr(chat_service, "mask_chunks", lambda *_args, **_kwargs: masked)
    # Phase 3.5 (quarantine drop) runs a real Postgres query and this module's
    # session factory yields `None` on purpose, to stay Postgres-free like
    # `mask_chunks` above; see test_chat_permissions_quarantine.py for the
    # real-database coverage of `_drop_quarantined_hits` itself.
    monkeypatch.setattr(chat_service, "_drop_quarantined_hits", lambda _db, hits: hits)
    # `_prepare_context` returns (masked_chunks, meta, counted, overview,
    # synthesis_block, recurrence_block) — Stage 4 added the counted and overview
    # tiers, W2.6 added the two fan-out blocks. Only `meta` is under test here, so
    # the rest are discarded by name rather than by arity: a bare `_, meta = ...`
    # broke silently when the tuple grew, whereas naming every member makes the
    # next growth a loud `ValueError` in exactly the place that has to be updated.
    _, meta, _counted, _overview, _synthesis, _recurrence = chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="what did we decide?",
        history=[],
        settings=ChatSettings(),
        file_uuids=None,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )
    return meta


def test_chunks_emptied_by_masking_are_counted(monkeypatch):
    masked = [
        MaskedChunk(source=_hit(0), content="kept"),
        MaskedChunk(source=_hit(1), content=""),
        MaskedChunk(source=_hit(2), content="   "),
    ]
    meta = _prepare(monkeypatch, masked=masked, retrieved=3)

    assert meta["chunks_dropped_empty_after_masking"] == 2
    assert meta["retrieved"] == 3


def test_counter_is_zero_when_masking_kept_everything(monkeypatch):
    masked = [MaskedChunk(source=_hit(index), content="kept") for index in range(3)]
    meta = _prepare(monkeypatch, masked=masked, retrieved=3)

    assert meta["chunks_dropped_empty_after_masking"] == 0


def test_a_fully_masked_retrieval_is_distinguishable_from_no_retrieval(monkeypatch):
    """The whole point: these two situations used to look identical in metadata."""
    all_masked = _prepare(
        monkeypatch,
        masked=[MaskedChunk(source=_hit(index), content="") for index in range(4)],
        retrieved=4,
    )
    nothing_found = _prepare(monkeypatch, masked=[], retrieved=0)

    assert all_masked["retrieved"] == 4
    assert all_masked["chunks_dropped_empty_after_masking"] == 4
    assert nothing_found["retrieved"] == 0
    assert nothing_found["chunks_dropped_empty_after_masking"] == 0
