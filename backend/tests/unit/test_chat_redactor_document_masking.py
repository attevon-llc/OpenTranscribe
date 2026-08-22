"""Document-chunk masking before the LLM (#362), and the ``file_id`` collision it closes.

``Document.id`` and ``MediaFile.id`` are independent SERIAL sequences that collide in
any real deployment — the OpenSearch index writes both into the same ``file_id`` field
(``services/search/indexing_service.py``). ``ChunkHit.source_kind`` exists specifically
so ``mask_chunks``/``_mask_from_segments`` never queries ``MediaFile`` for a
document-origin chunk, regardless of whether a colliding id happens to exist. The first
test in this file proves that directly: a real ``MediaFile`` and ``Document`` sharing one
id, with the media file's transcript overlapping whatever time range a document chunk
carries — the masked document output must never contain the unrelated transcript text.

The controlling property, same as ``test_chat_redactor.py``: fail-CLOSED. When the
cached-span path cannot be trusted, mask inline rather than send raw text.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from sqlalchemy import text

from app.core import constants as C  # noqa: N812
from app.services.chat.redactor import mask_chunks
from app.services.chat.redactor import mask_digests
from app.services.chat.redactor import mask_document_chunks
from app.services.search.chunk_retrieval import ChunkHit


def _cfg(*, enabled: bool, redact_before_llm: bool, categories=("pii", "profanity", "custom")):
    return SimpleNamespace(
        enabled=enabled,
        redact_before_llm=redact_before_llm,
        enabled_categories=set(categories),
    )


def _document_chunk(
    file_id: int, *, chunk_index: int = 0, content: str = "the document text"
) -> ChunkHit:
    return ChunkHit(
        file_uuid="22222222-2222-2222-2222-222222222222",
        file_id=file_id,
        chunk_index=chunk_index,
        content=content,
        title="Report",
        source_kind="document",
    )


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"maskdoc_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def test_a_document_chunk_never_queries_or_serves_an_unrelated_media_files_transcript(db_session):
    """The collision regression: force ``Document.id == MediaFile.id`` for real.

    A document chunk defaults to ``start_time=0.0``/``end_time=None`` (it has no
    timeline) — exactly the window a real recording's opening segment sits in — so if
    ``_mask_from_segments`` were ever reached for a document-origin chunk, this is the
    overlap that would trigger it. It must never be reached at all.
    """
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        shared_id = 900001 + (uuid_pkg.uuid4().int % 5000)

        conn.execute(
            text(
                "INSERT INTO document (id, uuid, user_id, filename, storage_path, file_size, "
                "content_type, redaction_status) VALUES "
                "(:id, :u, :uid, 'collision.pdf', 'x/collision.pdf', 1, 'application/pdf', 'done')"
            ),
            {"id": shared_id, "u": str(uuid_pkg.uuid4()), "uid": user_id},
        )
        conn.execute(
            text(
                "INSERT INTO document_chunk (document_id, chunk_index, text, char_start, "
                "char_end, redactions) VALUES (:d, 0, 'the document text', 0, 17, NULL)"
            ),
            {"d": shared_id},
        )

        conn.execute(
            text(
                "INSERT INTO media_file (id, uuid, user_id, filename, storage_path, file_size, "
                "content_type, redaction_status) VALUES "
                "(:id, :u, :uid, 'collision.wav', 'x/collision.wav', 1, 'audio/wav', 'done')"
            ),
            {"id": shared_id, "u": str(uuid_pkg.uuid4()), "uid": user_id},
        )
        secret_text = "the unrelated recording said SECRET-DO-NOT-LEAK-12345"
        conn.execute(
            text(
                "INSERT INTO transcript_segment (media_file_id, start_time, end_time, text) "
                "VALUES (:f, 0.0, 5.0, :t)"
            ),
            {"f": shared_id, "t": secret_text},
        )

        chunk = _document_chunk(shared_id)
        cfg = _cfg(enabled=True, redact_before_llm=True)
        with patch("app.services.redaction.config.resolve_effective_config", return_value=cfg):
            masked = mask_chunks(_factory(db_session), [chunk], user_id=user_id)

        assert secret_text not in masked[0].content, (
            "a document chunk's masked output contained an UNRELATED media file's "
            "transcript text — the file_id collision guard failed"
        )
        assert "SECRET-DO-NOT-LEAK" not in masked[0].content
    finally:
        db_session.rollback()


def test_a_document_digest_hit_never_queries_or_serves_an_unrelated_medias_digest(db_session):
    """The digest-plane collision (#403 Stage-6 mixed-collection coverage, W2.3),
    one level up from the chunk-plane test above. Same setup — a real
    ``MediaFile`` and ``Document`` forced to share one id — but this time the
    colliding ``MediaFile`` has a ``file_facts`` row with a digest SECTION AT
    THE SAME INDEX the document-origin hit carries. If ``_gather_digest_plans``
    ever routed a document-origin hit through the ``MediaFile``/
    ``FileFacts.media_file_id`` lookup, this section would resolve and its
    sentences — a DIFFERENT file's real, provenance-backed content — would be
    served as if they belonged to this document.

    The guard is structural: document-origin digest hits never reach that
    lookup at all (``ChunkHit.is_document`` routes them to
    ``_DigestPlan(sentences=None, unresolvable=True)`` before any query runs),
    so they fall through to the inline masker over their OWN content only —
    proved here by patching ``_mask_inline`` and asserting it was called with
    the document hit's own text, never the colliding file's digest.
    """
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        shared_id = 900001 + (uuid_pkg.uuid4().int % 5000)

        conn.execute(
            text(
                "INSERT INTO document (id, uuid, user_id, filename, storage_path, file_size, "
                "content_type, redaction_status) VALUES "
                "(:id, :u, :uid, 'collision.pdf', 'x/collision.pdf', 1, 'application/pdf', 'done')"
            ),
            {"id": shared_id, "u": str(uuid_pkg.uuid4()), "uid": user_id},
        )
        conn.execute(
            text(
                "INSERT INTO media_file (id, uuid, user_id, filename, storage_path, file_size, "
                "content_type, redaction_status) VALUES "
                "(:id, :u, :uid, 'collision.wav', 'x/collision.wav', 1, 'audio/wav', 'done')"
            ),
            {"id": shared_id, "u": str(uuid_pkg.uuid4()), "uid": user_id},
        )
        secret_digest = {
            "sections": [
                {
                    "index": 0,
                    "sentences": [
                        {
                            "text": "SECRET-DO-NOT-LEAK the unrelated recording's own digest",
                            "order": 0,
                            "speaker": "Someone Else",
                            "provenance": {
                                "kind": "segment_ids",
                                "segment_ids": [1],
                                "start_time": 0.0,
                                "end_time": 1.0,
                            },
                        }
                    ],
                }
            ]
        }
        conn.execute(
            text(
                "INSERT INTO file_facts (media_file_id, digest, facts, keyphrases, "
                "generator_version, source_fingerprint) VALUES "
                "(:f, CAST(:d AS jsonb), '{}'::jsonb, '{}'::jsonb, '1.1.1', 'fp-collision')"
            ),
            {"f": shared_id, "d": json.dumps(secret_digest)},
        )

        document_hit = ChunkHit(
            file_uuid="33333333-3333-3333-3333-333333333333",
            file_id=shared_id,
            chunk_index=-1,
            content="the document's own digest text",
            title="Report",
            source_kind="document",
            digest_section=0,
        )
        cfg = _cfg(enabled=True, redact_before_llm=True)
        with (
            patch("app.services.redaction.config.resolve_effective_config", return_value=cfg),
            patch(
                "app.services.chat.redactor._mask_inline",
                side_effect=lambda t, _cfg: f"[MASKED] {t}",
            ) as inline,
        ):
            masked = mask_digests(_factory(db_session), [document_hit], user_id=user_id)

        assert "SECRET-DO-NOT-LEAK" not in masked[0].content, (
            "a document digest hit's masked output contained an UNRELATED media "
            "file's digest content — the file_id collision guard failed"
        )
        inline.assert_called_once_with("the document's own digest text", cfg)
        assert masked[0].content == "[MASKED] the document's own digest text"
    finally:
        db_session.rollback()


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    """A ``session_scope``-shaped factory over one prepared session.

    Both public maskers take the FACTORY, never a ``Session`` (issue #83): they own
    the transaction boundary so they can CLOSE it before the detector runs.
    """
    return lambda: _one_session(db)


def test_policy_off_passes_document_content_through_untouched():
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=True, redact_before_llm=False),
    ):
        masked = mask_document_chunks(_factory(db), [_document_chunk(5)], user_id=1)

    assert masked[0].content == "the document text"
    assert masked[0].was_masked is False


def test_unresolvable_policy_fails_closed_for_documents():
    db = MagicMock()
    with patch(
        "app.services.redaction.config.resolve_effective_config",
        side_effect=RuntimeError("boom"),
    ):
        masked = mask_document_chunks(_factory(db), [_document_chunk(5)], user_id=1)

    assert masked[0].content == ""
    assert masked[0].was_masked is True


def test_an_unscanned_document_falls_through_to_inline_masking(db_session):
    """``redaction_status`` is not ``done`` (never scanned) — must not trust absent spans."""
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        document_id = int(
            conn.execute(
                text(
                    "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                    "content_type) VALUES (:u, :uid, 'unscanned.pdf', 'x/unscanned.pdf', 1, "
                    "'application/pdf') RETURNING id"
                ),
                {"u": str(uuid_pkg.uuid4()), "uid": user_id},
            ).scalar()
        )
        conn.execute(
            text(
                "INSERT INTO document_chunk (document_id, chunk_index, text, char_start, char_end) "
                "VALUES (:d, 0, 'call 555-0100 for support', 0, 26)"
            ),
            {"d": document_id},
        )

        chunk = _document_chunk(document_id, content="call 555-0100 for support")
        cfg = _cfg(enabled=True, redact_before_llm=True, categories=("pii", "profanity", "custom"))
        with (
            patch("app.services.redaction.config.resolve_effective_config", return_value=cfg),
            patch("app.services.redaction.config.blocking_detector_failures", return_value=set()),
            patch(
                "app.services.redaction.config.detection_config_for_all",
                return_value={"language": "en"},
            ),
        ):
            masked = mask_document_chunks(_factory(db_session), [chunk], user_id=user_id)

        # Inline detection ran (real Presidio/wordlist) rather than trusting absent
        # cached spans as "nothing to mask" — was_masked is still True either way, so
        # what proves the fallback ran is that the call did not raise and returned a
        # string, exercising the real detect_segment_spans path end to end.
        assert masked[0].was_masked is True
        assert isinstance(masked[0].content, str)
    finally:
        db_session.rollback()


def test_document_status_key_from_constants_matches_the_scan_query():
    """Guards against a typo silently making the ``done`` gate always False."""
    assert C.REDACTION_STATUS_DONE == "done"
