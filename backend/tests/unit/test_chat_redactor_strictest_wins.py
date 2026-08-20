"""Strictest-wins chat egress masking (task #40) — the T-matrix and its traps.

Chat's egress masking used to resolve a SINGLE subject's redaction policy: the
plan said the file owner, issue #402 shipped the requester. Both are wrong in
one direction — requester-subject lets a sharee with a permissive policy read
PII the owner meant to hide; owner-subject ignores a stricter requester-side
mandate. The fix (``union_effective_config`` in
``app/services/redaction/config.py``, wired through ``chat/redactor.py``) masks
if EITHER side's policy says to, resolved PER FILE, never once for the whole
turn.

Most tests here drive REAL Postgres and the REAL ``RedactionService.mask_segment``
against pre-cached ``TranscriptSegment.redactions`` spans — no Presidio cold load,
and no mocking of ``resolve_effective_config`` — every user's policy is a genuine
``UserSetting`` row read exactly the way production reads it. This is deliberate:
a ``resolve_effective_config`` patched with one fixed ``return_value`` can only
prove the union of a policy with itself, which ``union_effective_config``'s own
``a is b`` shortcut collapses to a no-op — it could never exercise a genuine
owner-vs-requester disagreement, which is the entire point of task #40. The two
explicit fail-closed tests are the exception: they need `resolve_effective_config`
itself to fail, which is a mock-only concern.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.chat.redactor import mask_chunks
from app.services.chat.redactor import mask_digests
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import most_restrictive_config
from app.services.redaction.config import union_effective_config
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

#: One segment carrying two DIFFERENT category spans, so a masker that only
#: honours ONE side's categories still leaves the other's target visible —
#: that gap is exactly what the mixed T-matrix cells exist to catch.
PHONE = "555-867-5309"
PROFANITY_WORD = "jerk"
CONTENT = f"Call Dana at {PHONE}, you {PROFANITY_WORD}."
_PHONE_START = CONTENT.index(PHONE)
_PHONE_END = _PHONE_START + len(PHONE)
_WORD_START = CONTENT.index(PROFANITY_WORD)
_WORD_END = _WORD_START + len(PROFANITY_WORD)


@contextmanager
def _one_session(db):
    yield db


def _factory(db):
    """A ``session_scope``-shaped factory over one prepared session."""
    return lambda: _one_session(db)


def _chunk(file_id: int, *, content: str = CONTENT, file_uuid: str | None = None) -> ChunkHit:
    return ChunkHit(
        file_uuid=file_uuid or str(uuid_pkg.uuid4()),
        file_id=file_id,
        chunk_index=0,
        content=content,
        title="Call",
        start_time=0.0,
        end_time=5.0,
    )


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"strictestwins_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _set_prefs(
    conn,
    user_id: int,
    *,
    enabled: bool,
    redact_before_llm: bool,
    categories: list[str],
    pii_entities: list[str] | None = None,
) -> None:
    """Write this user's REAL redaction ``UserSetting`` rows.

    Rolled back with the rest of ``db_session`` at test teardown — never a
    lasting change to the live dev data.
    """
    prefs = {
        "redaction_enabled": "true" if enabled else "false",
        "redaction_redact_before_llm": "true" if redact_before_llm else "false",
        "redaction_categories": json.dumps(categories),
    }
    if pii_entities is not None:
        prefs["redaction_pii_entities"] = json.dumps(pii_entities)
    for key, value in prefs.items():
        conn.execute(
            text(
                "INSERT INTO user_setting (uuid, user_id, setting_key, setting_value) "
                "VALUES (:u, :uid, :k, :v)"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id, "k": key, "v": value},
        )


def _neutralize_admin_redaction_floor(conn) -> None:
    """Force every ``redaction.force_*`` admin key off, inside THIS test's transaction.

    The T-matrix cells assert both an ON and an OFF outcome from user-level prefs
    alone, so a floor left on by an earlier manual admin action on this shared
    dev stack would silently make every cell mask, including the ones asserting
    it must NOT. Upserted, not inserted — the key may already exist. Rolled back
    with everything else in ``db_session``.
    """
    for key, value in (
        ("redaction.force_pii", "false"),
        ("redaction.force_toxicity", "false"),
        ("redaction.force_profanity", "false"),
        ("redaction.force_redact_before_llm", "false"),
    ):
        conn.execute(
            text(
                "INSERT INTO system_settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"k": key, "v": value},
        )


def _force_admin_redaction_floor(conn) -> None:
    """The admin mandate: PII masking, before every LLM call, for every user."""
    for key, value in (
        ("redaction.force_pii", "true"),
        ("redaction.force_redact_before_llm", "true"),
    ):
        conn.execute(
            text(
                "INSERT INTO system_settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"k": key, "v": value},
        )


def _new_file_with_segment(conn, owner_id: int, *, content: str = CONTENT) -> int:
    """A ``done``-scanned file owned by ``owner_id``, with ONE segment carrying
    both a ``pii`` span (the phone number) and a ``profanity`` span (the word)."""
    file_id = int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, redaction_status) VALUES "
                "(:u, :uid, 'sw.wav', 'x/sw.wav', 1, 'audio/wav', 'done') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": owner_id},
        ).scalar()
    )
    redactions = json.dumps(
        [
            {
                "char_start": _PHONE_START,
                "char_end": _PHONE_END,
                "category": "pii",
                "entity_type": "PHONE",
            },
            {
                "char_start": _WORD_START,
                "char_end": _WORD_END,
                "category": "profanity",
                "entity_type": "PROFANITY",
            },
        ]
    )
    conn.execute(
        text(
            "INSERT INTO transcript_segment (media_file_id, start_time, end_time, text, redactions) "
            "VALUES (:f, 0.0, 5.0, :t, CAST(:r AS jsonb))"
        ),
        {"f": file_id, "t": content, "r": redactions},
    )
    return file_id


def _new_digest_file(conn, owner_id: int, *, content: str = CONTENT) -> int:
    """A ``done``-scanned file with a ONE-section digest whose sentence's
    provenance points at a real segment carrying both category spans."""
    file_id = _new_file_with_segment(conn, owner_id, content=content)
    row = conn.execute(
        text("SELECT id FROM transcript_segment WHERE media_file_id = :f"), {"f": file_id}
    ).fetchone()
    segment_id = int(row[0])
    digest = {
        "sections": [
            {
                "index": 0,
                "sentences": [
                    {
                        "text": content,
                        "order": 0,
                        "speaker": None,
                        "provenance": {
                            "kind": "segment_ids",
                            "segment_ids": [segment_id],
                            "start_time": 0.0,
                            "end_time": 5.0,
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
            "(:f, CAST(:d AS jsonb), '{}'::jsonb, '{}'::jsonb, '1.1.1', :fp)"
        ),
        {"f": file_id, "d": json.dumps(digest), "fp": f"fp-{uuid_pkg.uuid4().hex[:8]}"},
    )
    return file_id


def _digest_hit(file_id: int) -> ChunkHit:
    return ChunkHit(
        file_uuid=str(uuid_pkg.uuid4()),
        file_id=file_id,
        chunk_index=-1,
        content=CONTENT,
        title="Weekly sync",
        start_time=0.0,
        end_time=5.0,
        digest_section=0,
    )


# --------------------------------------------------------------------------- #
# The T-matrix: owner {permissive, strict} x requester {permissive, strict}.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "owner_masks,requester_masks,cell",
    [
        (False, False, "both permissive -> unmasked"),
        (True, True, "both strict -> masked"),
        (True, False, "strict owner, permissive requester (#402's hole)"),
        (False, True, "permissive owner, strict requester (the plan's hole)"),
    ],
)
def test_t_matrix_owner_by_requester_pii_masking(db_session, owner_masks, requester_masks, cell):
    """The union in every cell — masked if EITHER side wants it, on real Postgres."""
    conn = db_session.connection()
    try:
        _neutralize_admin_redaction_floor(conn)
        owner_id = _new_user(conn)
        requester_id = _new_user(conn)
        _set_prefs(
            conn,
            owner_id,
            enabled=owner_masks,
            redact_before_llm=owner_masks,
            categories=["pii"],
            pii_entities=["PHONE"],
        )
        _set_prefs(
            conn,
            requester_id,
            enabled=requester_masks,
            redact_before_llm=requester_masks,
            categories=["pii"],
            pii_entities=["PHONE"],
        )
        file_id = _new_file_with_segment(conn, owner_id)

        masked = mask_chunks(_factory(db_session), [_chunk(file_id)], user_id=requester_id)

        should_mask = owner_masks or requester_masks
        if should_mask:
            assert PHONE not in masked[0].content, f"{cell}: expected PII masked"
            assert masked[0].was_masked is True, cell
        else:
            assert PHONE in masked[0].content, f"{cell}: expected PII to pass through"
            assert masked[0].was_masked is False, cell
    finally:
        db_session.rollback()


def test_the_union_masks_both_categories_not_just_the_stricter_sides_own():
    """ "Union of what they mask", not "whichever side masks more overall".

    Owner masks ONLY `pii`; requester masks ONLY `profanity`. Strictest-wins
    must mask BOTH — an implementation that picked "the stricter policy" as a
    whole (rather than unioning categories) would still leak one of the two.
    """
    owner_cfg = EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        enabled_categories={"pii"},
        pii_entities={"PHONE"},
    )
    requester_cfg = EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        enabled_categories={"profanity"},
    )
    effective = union_effective_config(requester_cfg, owner_cfg)

    from app.services.redaction.service import RedactionService

    spans = [
        {
            "char_start": _PHONE_START,
            "char_end": _PHONE_END,
            "category": "pii",
            "entity_type": "PHONE",
        },
        {
            "char_start": _WORD_START,
            "char_end": _WORD_END,
            "category": "profanity",
            "entity_type": "PROFANITY",
        },
    ]
    masked_text, _applied = RedactionService.mask_segment(CONTENT, spans, None, effective, set())

    assert PHONE not in masked_text, "owner's pii category must survive the union"
    assert PROFANITY_WORD not in masked_text, (
        "requester's profanity category must survive the union"
    )


# --------------------------------------------------------------------------- #
# Per-file resolution: a multi-owner scope must not over-mask the permissive file.
# --------------------------------------------------------------------------- #


def test_multi_file_scope_resolves_strictest_wins_per_file_not_globally(db_session):
    """The test that distinguishes PER-FILE union from a lazy GLOBAL union.

    One requester, one turn, two files from two DIFFERENT owners: one strict,
    one permissive. A global union (strictest owner in the whole scope wins for
    every file) would mask BOTH files' PII. Per-file resolution must mask only
    the strict owner's file and leave the permissive owner's file exactly as
    the requester's own (permissive) policy would.
    """
    conn = db_session.connection()
    try:
        _neutralize_admin_redaction_floor(conn)
        requester_id = _new_user(conn)
        _set_prefs(
            conn,
            requester_id,
            enabled=False,
            redact_before_llm=False,
            categories=["pii"],
            pii_entities=["PHONE"],
        )

        strict_owner_id = _new_user(conn)
        _set_prefs(
            conn,
            strict_owner_id,
            enabled=True,
            redact_before_llm=True,
            categories=["pii"],
            pii_entities=["PHONE"],
        )
        permissive_owner_id = _new_user(conn)
        _set_prefs(
            conn,
            permissive_owner_id,
            enabled=False,
            redact_before_llm=False,
            categories=["pii"],
            pii_entities=["PHONE"],
        )

        strict_file_id = _new_file_with_segment(conn, strict_owner_id)
        permissive_file_id = _new_file_with_segment(conn, permissive_owner_id)

        masked = mask_chunks(
            _factory(db_session),
            [_chunk(strict_file_id), _chunk(permissive_file_id)],
            user_id=requester_id,
        )

        strict_result, permissive_result = masked
        assert PHONE not in strict_result.content, "strict owner's file must be masked"
        assert strict_result.was_masked is True
        assert PHONE in permissive_result.content, (
            "permissive owner's file must NOT be over-masked just because another "
            "file in the same turn belongs to a strict owner"
        )
        assert permissive_result.was_masked is False
    finally:
        db_session.rollback()


# --------------------------------------------------------------------------- #
# Digest path: same union, applied through provenance (never mask_chunks).
# --------------------------------------------------------------------------- #


def test_digest_path_masks_under_the_effective_union(db_session):
    """The mixed cell, through `mask_digests` instead of `mask_chunks`."""
    conn = db_session.connection()
    try:
        _neutralize_admin_redaction_floor(conn)
        owner_id = _new_user(conn)
        _set_prefs(
            conn,
            owner_id,
            enabled=True,
            redact_before_llm=True,
            categories=["pii"],
            pii_entities=["PHONE"],
        )
        requester_id = _new_user(conn)
        _set_prefs(
            conn,
            requester_id,
            enabled=False,
            redact_before_llm=False,
            categories=["pii"],
            pii_entities=["PHONE"],
        )
        file_id = _new_digest_file(conn, owner_id)

        masked = mask_digests(_factory(db_session), [_digest_hit(file_id)], user_id=requester_id)

        assert PHONE not in masked[0].content, (
            "the file owner's stricter policy must still mask the digest even "
            "though the requester's own policy would not have"
        )
        assert masked[0].was_masked is True
    finally:
        db_session.rollback()


def test_digest_path_passes_through_when_neither_side_wants_masking(db_session):
    conn = db_session.connection()
    try:
        _neutralize_admin_redaction_floor(conn)
        owner_id = _new_user(conn)
        _set_prefs(conn, owner_id, enabled=False, redact_before_llm=False, categories=["pii"])
        requester_id = _new_user(conn)
        _set_prefs(conn, requester_id, enabled=False, redact_before_llm=False, categories=["pii"])
        file_id = _new_digest_file(conn, owner_id)

        masked = mask_digests(_factory(db_session), [_digest_hit(file_id)], user_id=requester_id)

        assert masked[0].content == CONTENT
        assert masked[0].was_masked is False
    finally:
        db_session.rollback()


# --------------------------------------------------------------------------- #
# Fail closed: an unresolvable owner policy must mask, never pass through.
# --------------------------------------------------------------------------- #


def _db_with_owner(status, segments, *, owner_user_id, coverage=None, language="en"):
    """A MagicMock session whose scan row names ``owner_user_id`` as the file's
    owner, for the two failure-injection tests below (real Postgres cannot
    easily be made to raise `resolve_effective_config` on demand)."""
    db = MagicMock()
    scan_q = MagicMock()
    scan_q.filter.return_value.first.return_value = SimpleNamespace(
        id=1,
        redaction_status=status,
        redaction_coverage=coverage,
        language=language,
        user_id=owner_user_id,
    )
    seg_q = MagicMock()
    seg_q.filter.return_value.order_by.return_value.all.return_value = segments
    db.query.side_effect = [scan_q, seg_q]
    return db


def _real_cfg(*, enabled, redact_before_llm, categories, pii_entities=None):
    return EffectiveRedactionConfig(
        enabled=enabled,
        redact_before_llm=redact_before_llm,
        enabled_categories=set(categories),
        pii_entities=set(pii_entities or []),
    )


def test_mock_mixed_cell_strict_owner_permissive_requester_masks():
    """DB-free twin of the T-matrix's #402 cell — no Postgres required.

    Owner (999) is strict; requester (1) is fully permissive. Real cached
    ``pii``/``PHONE`` span, real ``RedactionService.mask_segment`` (not
    mocked): strictest-wins must still mask, closing the exact hole issue #402
    shipped (a permissive requester reading PII a stricter owner hides).
    """
    owner_strict = _real_cfg(
        enabled=True, redact_before_llm=True, categories=["pii"], pii_entities=["PHONE"]
    )
    requester_permissive = _real_cfg(enabled=False, redact_before_llm=False, categories=[])

    def _resolve(_db, user_id):
        return owner_strict if user_id == 999 else requester_permissive

    segment = SimpleNamespace(
        text=CONTENT,
        redactions=[
            {
                "char_start": _PHONE_START,
                "char_end": _PHONE_END,
                "category": "pii",
                "entity_type": "PHONE",
            }
        ],
        words=None,
    )
    db = _db_with_owner("done", [segment], owner_user_id=999)

    with patch("app.services.redaction.config.resolve_effective_config", side_effect=_resolve):
        masked = mask_chunks(_factory(db), [_chunk(5)], user_id=1)

    assert PHONE not in masked[0].content, (
        "strict owner must still be honoured over a permissive requester"
    )
    assert masked[0].was_masked is True


def test_mock_mixed_cell_permissive_owner_strict_requester_masks():
    """DB-free twin of the T-matrix's "original plan" cell — no Postgres required.

    Owner (999) is fully permissive; requester (1) is strict. An owner-subject
    implementation (the original plan) would pass this straight through.
    Strictest-wins must still mask.
    """
    owner_permissive = _real_cfg(enabled=False, redact_before_llm=False, categories=[])
    requester_strict = _real_cfg(
        enabled=True, redact_before_llm=True, categories=["pii"], pii_entities=["PHONE"]
    )

    def _resolve(_db, user_id):
        return owner_permissive if user_id == 999 else requester_strict

    segment = SimpleNamespace(
        text=CONTENT,
        redactions=[
            {
                "char_start": _PHONE_START,
                "char_end": _PHONE_END,
                "category": "pii",
                "entity_type": "PHONE",
            }
        ],
        words=None,
    )
    db = _db_with_owner("done", [segment], owner_user_id=999)

    with patch("app.services.redaction.config.resolve_effective_config", side_effect=_resolve):
        masked = mask_chunks(_factory(db), [_chunk(5)], user_id=1)

    assert PHONE not in masked[0].content, (
        "strict requester must still be honoured over a permissive owner"
    )
    assert masked[0].was_masked is True


def test_fail_closed_when_the_owners_policy_read_raises():
    """ "Missing user, DB error" (task #40): the owner id resolves, but reading
    THEIR policy raises. Masking must still apply — never fall through to only
    the requester's (permissive) policy, which would look identical to "the
    owner permits it".

    Drives the CACHED-SPAN path (real ``RedactionService.mask_segment``, not
    mocked): the segment already carries the real ``pii``/``PHONE`` span most
    ``most_restrictive_config()`` would mask, so the assertion is on genuine
    masked output text, not on a mock having been called.
    """
    requester_permissive = SimpleNamespace(
        enabled=False,
        redact_before_llm=False,
        redact_before_llm_locked=False,
        enabled_categories=set(),
    )

    def _resolve(_db, user_id):
        if user_id == 999:  # the owner
            raise RuntimeError("db down resolving the owner's policy")
        return requester_permissive  # the requester

    segment = SimpleNamespace(
        text=CONTENT,
        redactions=[
            {
                "char_start": _PHONE_START,
                "char_end": _PHONE_END,
                "category": "pii",
                "entity_type": "PHONE",
            }
        ],
        words=None,
    )
    db = _db_with_owner("done", [segment], owner_user_id=999)

    with patch("app.services.redaction.config.resolve_effective_config", side_effect=_resolve):
        masked = mask_chunks(_factory(db), [_chunk(5)], user_id=1)

    assert PHONE not in masked[0].content, (
        "an owner whose policy could not be read must fail closed to masking, "
        "not fall through to the requester's permissive policy alone"
    )
    assert masked[0].was_masked is True


def test_fail_closed_when_the_owner_cannot_be_identified_at_all():
    """The scan row itself is missing (file not found / no ``user_id`` column
    value) — no owner id to even attempt a resolve with. Must still fail
    closed, not read as "no owner opinion, use the requester's alone".

    The missing scan row also means the cached-span path declines, so this
    drives the INLINE fallback: the (fast, controlled) detector stand-in
    reports a real ``pii``/``PHONE`` span and the REAL
    ``RedactionService.mask_segment`` applies it — genuine masked text, not a
    mocked one.
    """
    requester_permissive = SimpleNamespace(
        enabled=False,
        redact_before_llm=False,
        redact_before_llm_locked=False,
        enabled_categories=set(),
    )
    db = MagicMock()
    db.query.side_effect = None
    db.query.return_value.filter.return_value.first.return_value = None  # no scan row at all

    def _detect(text_in, _words, _det_cfg, *, failures=None, **_kwargs):  # noqa: ARG001
        return (
            [
                {
                    "char_start": _PHONE_START,
                    "char_end": _PHONE_END,
                    "category": "pii",
                    "entity_type": "PHONE",
                }
            ],
            None,
        )

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=requester_permissive,
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_detect,
        ),
    ):
        masked = mask_chunks(_factory(db), [_chunk(5)], user_id=1)

    assert PHONE not in masked[0].content, (
        "an unidentifiable owner must fail closed to masking, never pass raw content through"
    )
    assert masked[0].was_masked is True


def test_most_restrictive_config_is_the_union_identity_a_permissive_side_cannot_beat():
    """Direct, DB-free pin of the fail-closed stand-in itself: unioning ANY
    permissive policy with it still masks pii/toxicity/profanity/custom."""
    permissive = EffectiveRedactionConfig(enabled=False, redact_before_llm=False)

    effective = union_effective_config(permissive, most_restrictive_config())

    assert effective.enabled is True
    assert effective.redact_before_llm is True
    assert effective.enabled_categories == {"pii", "toxicity", "profanity", "custom"}


# --------------------------------------------------------------------------- #
# The admin force floor cannot be reduced by either party.
# --------------------------------------------------------------------------- #


def test_admin_force_floor_survives_the_union_even_when_both_sides_are_permissive(db_session):
    """Neither the owner's nor the requester's own preference can shed an
    admin-mandated category — real Postgres, real admin `SystemSettings`."""
    conn = db_session.connection()
    try:
        _force_admin_redaction_floor(conn)
        owner_id = _new_user(conn)
        _set_prefs(conn, owner_id, enabled=False, redact_before_llm=False, categories=[])
        requester_id = _new_user(conn)
        _set_prefs(conn, requester_id, enabled=False, redact_before_llm=False, categories=[])
        file_id = _new_file_with_segment(conn, owner_id)

        masked = mask_chunks(_factory(db_session), [_chunk(file_id)], user_id=requester_id)

        assert PHONE not in masked[0].content, (
            "the admin force floor must win even though both the owner's and "
            "the requester's own preferences are fully permissive"
        )
        assert masked[0].was_masked is True
    finally:
        db_session.rollback()


def test_union_locked_categories_cannot_be_reduced_by_the_other_side():
    """Unit-level pin of `union_effective_config` directly: a category EITHER
    side has locked stays locked in the union, even if the other side's
    (possibly stale) config disagrees."""
    locked_by_admin = EffectiveRedactionConfig(
        enabled=True,
        redact_before_llm=True,
        redact_before_llm_locked=True,
        enabled_categories={"pii"},
        locked_categories={"pii"},
    )
    unaware_of_the_lock = EffectiveRedactionConfig(
        enabled=False,
        redact_before_llm=False,
        redact_before_llm_locked=False,
        enabled_categories=set(),
        locked_categories=set(),
    )

    effective = union_effective_config(locked_by_admin, unaware_of_the_lock)

    assert "pii" in effective.locked_categories
    assert effective.redact_before_llm_locked is True
