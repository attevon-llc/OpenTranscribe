"""Every OpenSearch document writer stamps the file's trash state from the row (#1101).

The trash stamp (``availability_stamp``) rewrites a file's documents at trash and restore, but a
document written LATER (a reindex, a digest rebuild after a rename, a summary re-index, a
voiceprint) must carry the right value on its own, or a reindex of a trashed file would bring
it back into search, chat and speaker matching. So:

* the chunk-plane writers (chunks, digests, summary leaves) and the legacy ``transcripts``
  document carry ``availability`` as the callers resolved it from the row
  (``file_availability.index_availability``), and every production caller passes it (AST
  guard below: the parameter has a default for the many test callers, so the type checker
  alone would not catch a production call that forgot it);
* the per-file voiceprint writers read it themselves, because their dozen callers hold only a
  ``media_file_id``.
"""

from __future__ import annotations

import ast
import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.media import MediaFile

APP = Path(__file__).resolve().parents[2] / "app"

#: Writers whose production callers must pass ``availability=`` explicitly.
_WRITERS = {"index_transcript_chunks", "reindex_transcript", "index_transcript"}
#: Writers that take the per-file ``base_metadata`` dict, which must carry the field.
_PLANE_WRITERS = {"_index_digest_plane", "_index_summary_plane"}


def _calls(tree: ast.AST) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]]:
    found = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in _WRITERS | _PLANE_WRITERS:
                    found.append((func, node))
    return found


def _names_availability(func: ast.AST) -> bool:
    """A dict literal in ``func`` keyed by ``AVAILABILITY_FIELD`` or ``"availability"``."""
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Name) and key.id == "AVAILABILITY_FIELD":
                    return True
                if isinstance(key, ast.Constant) and key.value == "availability":
                    return True
    return False


def scan(source: str, relpath: str) -> list[str]:
    """``<relpath>::<function>`` for each writer call that does not carry ``availability``."""
    offenders = []
    for func, call in _calls(ast.parse(source)):
        name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
        if name in _WRITERS:
            ok = any(kw.arg == "availability" for kw in call.keywords)
        else:
            ok = _names_availability(func)
        if not ok:
            offenders.append(f"{relpath}::{func.name}")
    return offenders


def _app_offenders() -> tuple[list[str], int]:
    offenders: list[str] = []
    seen = 0
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text()
        if not any(w in source for w in _WRITERS | _PLANE_WRITERS):
            continue
        rel = path.relative_to(APP).as_posix()
        seen += len(_calls(ast.parse(source)))
        offenders += scan(source, rel)
    return sorted(set(offenders)), seen


def test_every_production_writer_call_carries_availability():
    offenders, seen = _app_offenders()
    assert seen >= 8, f"the sweep found only {seen} writer calls; it is checking nothing"
    assert not offenders, (
        "These write index documents without the file's availability, so a reindex of a "
        "trashed file would bring it back. Pass availability=meta['availability'] (from "
        "file_availability.index_availability), or put AVAILABILITY_FIELD in base_metadata:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scanner_fires_on_a_call_without_it_and_stays_clean_with_it():
    bad = "def f(s, m):\n    s.index_transcript_chunks(file_id=1, organization_id=None)\n"
    good = "def f(s, m):\n    s.reindex_transcript(file_id=1, availability=m['availability'])\n"
    plane_bad = "def f(s):\n    s._index_summary_plane(base_metadata={'title': 't'})\n"
    plane_good = (
        "def f(s, m):\n    s._index_summary_plane(base_metadata={AVAILABILITY_FIELD: m['a']})\n"
    )
    assert scan(bad, "x.py") == ["x.py::f"]
    assert scan(plane_bad, "x.py") == ["x.py::f"]
    assert scan(good, "x.py") == [] and scan(plane_good, "x.py") == []


# --- the chunk plane writes what it is given ------------------------------------------------


@pytest.fixture
def captured(monkeypatch) -> dict[str, Any]:
    """Run ``index_transcript_chunks`` with every collaborator stubbed; capture what it writes."""
    from app.services.search import indexing_service

    for name, value in (
        ("is_neural_pipeline_available", lambda: False),
        ("ensure_chunks_index_exists", lambda: True),
        ("ensure_search_pipeline_exists", lambda: True),
        ("get_opensearch_client", lambda: object()),
    ):
        monkeypatch.setattr(indexing_service, name, value)
    service = indexing_service.TranscriptIndexingService
    out: dict[str, Any] = {}

    def _bulk(self, chunks, use_neural_pipeline):
        out["chunks"] = chunks
        return len(chunks)

    def _plane(key):
        def _record(self, **kwargs):
            out[key] = kwargs["base_metadata"]
            return 0

        return _record

    monkeypatch.setattr(service, "_bulk_index_chunks", _bulk)
    monkeypatch.setattr(service, "_prune_stale_chunks", lambda self, file_uuid, keep_count: 0)
    monkeypatch.setattr(service, "_index_digest_plane", _plane("digest"))
    monkeypatch.setattr(service, "_index_summary_plane", _plane("summary"))
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", lambda: None)
    return out


def test_a_trashed_files_chunks_digests_and_summary_leaves_are_written_trashed(captured):
    from app.services.search.indexing_service import TranscriptIndexingService

    TranscriptIndexingService().index_transcript_chunks(
        file_id=1,
        file_uuid="22222222-2222-4222-8222-222222222222",
        user_id=1,
        segments=[{"start": 0.0, "end": 4.0, "text": "the budget review", "speaker": "S0"}],
        title="t",
        speakers=["S0"],
        tags=[],
        availability="trashed",
    )

    assert captured["chunks"] and {c["availability"] for c in captured["chunks"]} == {"trashed"}
    assert captured["digest"]["availability"] == "trashed"
    assert captured["summary"]["availability"] == "trashed"


def test_the_legacy_transcript_document_carries_the_stamp(monkeypatch):
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import transcripts

    written: list[dict[str, Any]] = []
    monkeypatch.setattr(os_client, "opensearch_client", SimpleNamespace(index=None))
    monkeypatch.setattr(transcripts, "ensure_indices_exist", lambda: None)

    def _write(_fn: Any, **kw: Any) -> dict[str, Any]:
        written.append(kw["body"])
        return {}

    monkeypatch.setattr(os_client, "call_idempotent_write", _write)

    transcripts.index_transcript(
        1, "u-1", 1, "text", [], "t", organization_id=None, availability="trashed"
    )

    assert written[0]["availability"] == "trashed"


# --- the rows -------------------------------------------------------------------------------


def _file(db, owner, *, trashed: bool) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"writer_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
    )
    if trashed:
        media_file.deleted_at = datetime.now(UTC)
        media_file.purge_after = datetime.now(UTC) + timedelta(days=30)
        media_file.trash_origin = "user"
    db.add(media_file)
    db.flush()
    return media_file


def test_the_index_metadata_reads_the_trash_state_from_the_row(db_session, normal_user):
    from app.tasks.search_indexing_task import extract_file_index_metadata

    trashed = _file(db_session, normal_user, trashed=True)
    live = _file(db_session, normal_user, trashed=False)

    assert extract_file_index_metadata(db_session, trashed, trashed.id)["availability"] == (
        "trashed"
    )
    assert extract_file_index_metadata(db_session, live, live.id)["availability"] == "available"


# --- the voiceprint writers read it themselves ----------------------------------------------


@pytest.fixture
def speaker_writes(monkeypatch, db_session):
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import speaker_write

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    monkeypatch.setattr(speaker_write, "assert_voiceprint_write_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(speaker_write, "_declined_files_of", lambda _d: set())
    monkeypatch.setattr(speaker_write, "has_voiceprint_consent_hooks", lambda: False)
    monkeypatch.setattr(speaker_write, "ensure_indices_exist", lambda: None)
    docs: list[dict[str, Any]] = []

    class _Client:
        def index(self, **kwargs):
            docs.append(kwargs["body"])
            return {"result": "created"}

        def bulk(self, body):
            docs.extend(b for b in body if "index" not in b)
            return {"errors": False}

    monkeypatch.setattr(os_client, "opensearch_client", _Client())
    return docs


def test_every_voiceprint_writer_stamps_the_files_trash_state(
    speaker_writes, db_session, normal_user
):
    from app.core.embedding_models import PYANNOTE_EMBEDDING_DIMENSION_V4
    from app.services.opensearch_service import speaker_write

    trashed = _file(db_session, normal_user, trashed=True)
    live = _file(db_session, normal_user, trashed=False)
    vector = [0.1] * PYANNOTE_EMBEDDING_DIMENSION_V4
    base = {"speaker_id": 1, "user_id": normal_user.id, "name": "S", "embedding": vector}

    speaker_write.add_speaker_embedding_v4(speaker_uuid="s-1", media_file_id=trashed.id, **base)
    speaker_write.add_speaker_embedding(speaker_uuid="s-2", media_file_id=live.id, **base)
    speaker_write.bulk_add_speaker_embeddings_v4(
        [
            {**base, "speaker_uuid": "s-3", "media_file_id": trashed.id},
            {**base, "speaker_uuid": "s-4", "media_file_id": None},
        ]
    )

    assert [d.get("availability") for d in speaker_writes] == [
        "trashed",
        "available",
        "trashed",
        None,  # a document of no file is no file's to hide
    ]
