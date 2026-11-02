"""The erasure receipt against the real stores (issue #1353, plan tests R5-R11).

A committed subject is seeded into Postgres, MinIO and OpenSearch, erased through the real
``erase_user`` / ``erase_org_member_data``, and the receipt is checked against probes that share
no code with the measurers: a direct ``list_objects(include_version=True)``, a direct index
``_count``, a direct ``SELECT count(*)``. Needs the live stack (MinIO and OpenSearch reachable).
"""

from __future__ import annotations

import json
import os
import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from tests.integration.erasure_receipt_world import SPEAKER_INDEX
from tests.integration.erasure_receipt_world import World
from tests.integration.erasure_receipt_world import build_world
from tests.integration.erasure_receipt_world import cleanup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("erasure_receipt_e2e"),
    pytest.mark.skipif(
        os.environ.get("SKIP_S3") == "True" or os.environ.get("SKIP_OPENSEARCH") == "True",
        reason="needs the live MinIO and OpenSearch",
    ),
]


@pytest.fixture()
def journal(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def world(journal):
    built: list[World] = []

    def _make(label: str = "user", **kw) -> World:
        w = build_world(label, **kw)
        built.append(w)
        return w

    yield _make
    for w in built:
        cleanup(w)


def _session():
    from app.db.base import SessionLocal

    return SessionLocal()


def _erase_user(user_id: int) -> dict:
    from app.services.gdpr_erasure_service import erase_user

    db = _session()
    try:
        return erase_user(db, user_id)
    finally:
        db.close()


def _receipts(ledger_uuid: str) -> list:
    from app.models.erasure import ErasureLedgerEntry
    from app.models.erasure import ErasureReceipt

    db = _session()
    try:
        entry = db.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == ledger_uuid).one()
        rows = (
            db.query(ErasureReceipt)
            .filter(ErasureReceipt.ledger_entry_id == entry.id)
            .order_by(ErasureReceipt.id)
            .all()
        )
        for r in rows:
            db.expunge(r)
        db.expunge(entry)
        return [entry, *rows]
    finally:
        db.close()


def _count(sql: str, **params) -> int:
    db = _session()
    try:
        return int(db.execute(text(sql), params).scalar_one())
    finally:
        db.close()


def _object_versions(bucket: str, prefix: str) -> int:
    from app.services.minio_service import minio_client

    return sum(
        1
        for _ in minio_client.list_objects(
            bucket, prefix=prefix, recursive=True, include_version=True
        )
    )


def _os_count(index: str, query: dict) -> int:
    from app.services.opensearch_service import opensearch_client

    opensearch_client.indices.refresh(index=index)
    return int(opensearch_client.count(index=index, body={"query": query})["count"])


def test_user_erasure_receipt_is_verified(world):
    from app.core.config import settings

    w = world()
    summary = _erase_user(w.user_id)
    entry, receipt = _receipts(summary["ledger_uuid"])[:2]

    assert receipt.outcome == "verified", (
        {k: v for k, v in receipt.residual.items() if v},
        receipt.unverified,
    )
    assert summary["complete"] is True
    assert summary["receipt"]["outcome"] == "verified"
    assert entry.status == "complete"
    assert all(v == 0 for v in receipt.residual.values()) and receipt.residual
    assert not receipt.unverified

    # What was seeded is what the receipt says was deleted.
    assert receipt.deleted["pg.media_file"] == 2
    assert receipt.deleted["pg.user"] == 1
    assert receipt.deleted["pg.speaker"] == 2
    assert receipt.deleted["pg.comment"] == 1
    assert receipt.deleted["pg.chat_conversation"] == 1
    assert receipt.deleted["os.transcripts"] == 2
    assert receipt.deleted["os.transcript_chunks"] == 2
    assert receipt.deleted["os.speakers"] >= 2
    assert receipt.deleted["s3.media.temp"] == 2
    assert receipt.deleted["s3.cache.derived"] == 2
    assert receipt.deleted["s3.media.avatars"] >= 1
    assert receipt.deleted["s3.media.file_objects"] >= 6, "stray objects must be counted too"
    assert receipt.deleted["redis.lockout"] == 1

    # Independent probes.
    media = settings.MEDIA_BUCKET_NAME
    assert _object_versions(media, f"user_{w.user_id}/") == 0
    assert _object_versions(media, f"avatars/{w.user_id}/") == 0
    for fuuid in w.file_uuids:
        assert _object_versions(media, f"temp/preprocess/{fuuid}/") == 0
    for fid in w.file_ids:
        assert _object_versions(settings.CACHE_BUCKET_NAME, f"derived/{fid}_") == 0
    assert _os_count(settings.OPENSEARCH_TRANSCRIPT_INDEX, {"ids": {"values": w.file_uuids}}) == 0
    assert _os_count(settings.OPENSEARCH_CHUNKS_INDEX, {"terms": {"file_uuid": w.file_uuids}}) == 0
    assert _os_count(SPEAKER_INDEX, {"ids": {"values": w.speaker_uuids}}) == 0
    assert _count('SELECT count(*) FROM "user" WHERE id = :i', i=w.user_id) == 0
    assert _count("SELECT count(*) FROM media_file WHERE user_id = :i", i=w.user_id) == 0
    assert _count("SELECT count(*) FROM comment WHERE user_id = :i", i=w.user_id) == 0

    from app.auth import lockout

    assert lockout.identifier_has_record(w.email) is False

    # The receipt, the summary and the journal hold no personal data.
    serialized = json.dumps(
        {
            "row": {
                c: str(getattr(receipt, c))
                for c in ("deleted", "retained", "residual", "unverified", "digest", "outcome")
            },
            "summary": summary["receipt"],
        }
    )
    journal_files = _journal_files()
    journal_text = "\n".join(p.read_text() for p in journal_files)
    for needle in (w.email, "e2e-delres-", *w.file_uuids, w.profile_uuid):
        assert needle not in serialized
        assert needle not in journal_text
    assert journal_files, "the journal must exist, or the scan above proves nothing"
    assert '"kind":"receipt"' in journal_text


def _journal_files():
    from app.services import erasure_ledger_service as ledger

    path = ledger.journal_path()
    return [path] if path.exists() else []


def test_residual_makes_the_erasure_partial(world, monkeypatch):
    """Make the prefix sweep refuse: the stray objects remain, so the receipt must say so."""
    from app.services.erasure_receipt import destroy_extras

    monkeypatch.setattr(destroy_extras, "erase_object_prefixes", lambda *a, **k: None)
    w = world()
    summary = _erase_user(w.user_id)
    entry, receipt = _receipts(summary["ledger_uuid"])[:2]

    assert receipt.outcome == "residual"
    assert receipt.residual["s3.media.file_objects"] >= 2
    assert summary["complete"] is False
    assert entry.status == "deferred"
    assert summary["receipt"]["residual_total"] >= 2


def test_unmeasurable_store_is_unverified_not_zero(world, monkeypatch):
    from app.services.erasure_receipt import stores_external

    def refuse():
        raise RuntimeError("OpenSearch client unavailable")

    w = world()
    monkeypatch.setattr(stores_external, "_os_client", refuse)
    summary = _erase_user(w.user_id)
    receipt = _receipts(summary["ledger_uuid"])[1]

    assert receipt.outcome == "unverified"
    assert "os.transcripts" in receipt.unverified
    assert "os.transcripts" not in receipt.residual, "unmeasurable must never read as zero"
    assert summary["complete"] is False


def test_unclassified_index_blocks_verification_until_allow_listed(world):
    from app.services.opensearch_service import opensearch_client
    from app.services.system_settings_service import set_setting

    w = world()
    w2 = world("allow")
    stray = f"test_erasure_receipt_stray_{uuid_pkg.uuid4().hex[:12]}"
    opensearch_client.indices.create(index=stray)
    try:
        summary = _erase_user(w.user_id)
        receipt = _receipts(summary["ledger_uuid"])[1]
        assert receipt.outcome == "unverified"
        assert "os.unclassified" in receipt.unverified

        db = _session()
        try:
            set_setting(db, "erasure.receipt.ignored_indices", json.dumps([stray]))
            summary2 = _erase_user(w2.user_id)
            receipt2 = _receipts(summary2["ledger_uuid"])[1]
        finally:
            set_setting(db, "erasure.receipt.ignored_indices", "[]")
            db.close()
        assert receipt2.outcome == "verified", receipt2.unverified
    finally:
        opensearch_client.indices.delete(index=stray, ignore_unavailable=True)


def test_delayed_reverify_catches_a_racing_write(world):
    from app.core.config import settings
    from app.services.erasure_receipt.flow import reverify_payload
    from app.services.erasure_receipt.types import Captured
    from app.services.erasure_receipt.types import ErasureScope
    from app.services.opensearch_service import opensearch_client
    from app.tasks.erasure_receipt_reverify import erasure_receipt_reverify

    w = world()
    summary = _erase_user(w.user_id)
    assert _receipts(summary["ledger_uuid"])[0].status == "complete"
    # A debounced reindex lands after the immediate receipt.
    opensearch_client.index(
        index=settings.OPENSEARCH_TRANSCRIPT_INDEX, id=w.file_uuids[0], body={"user_id": w.user_id}
    )
    opensearch_client.indices.refresh(index=settings.OPENSEARCH_TRANSCRIPT_INDEX)

    subject = Captured(
        file_ids=w.file_ids,
        file_uuids=w.file_uuids,
        file_owners=[(w.user_id, f) for f in w.file_ids],
        speaker_uuids=w.speaker_uuids,
    )
    scope = ErasureScope("user", user_id=w.user_id)
    result = erasure_receipt_reverify.run(
        ledger_uuid=summary["ledger_uuid"], **reverify_payload(scope, subject)
    )
    entry, immediate, delayed = _receipts(summary["ledger_uuid"])
    assert (immediate.phase, immediate.outcome) == ("immediate", "verified")
    assert (delayed.phase, delayed.outcome) == ("delayed", "residual")
    assert delayed.residual["os.transcripts"] == 1
    assert result["status"] == "residual"
    assert entry.status == "deferred", "a delayed leftover must re-open the ledger entry"


def test_legal_hold_is_retained_not_residual(world):
    w = world()
    db = _session()
    try:
        db.execute(
            text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": w.file_ids[0]}
        )
        db.commit()
    finally:
        db.close()
    summary = _erase_user(w.user_id)
    entry, receipt = _receipts(summary["ledger_uuid"])[:2]

    assert receipt.retained["pg.media_file.legal_hold"] == 1
    assert receipt.retained["pg.user"] == 1, "the account survives while a held file exists"
    assert receipt.residual["pg.media_file"] == 0
    assert receipt.outcome == "verified", (
        {k: v for k, v in receipt.residual.items() if v},
        receipt.unverified,
    )
    assert entry.status == "deferred" and entry.deferred_reason == "legal_hold"
    from app.core.config import settings

    held_prefix = f"user_{w.user_id}/file_{w.file_ids[0]}/"
    assert _object_versions(settings.MEDIA_BUCKET_NAME, held_prefix) >= 1, "held objects stay"
    other_prefix = f"user_{w.user_id}/file_{w.file_ids[1]}/"
    assert _object_versions(settings.MEDIA_BUCKET_NAME, other_prefix) == 0


def test_org_member_receipt_counts_de_attributed_tenant_vocabulary(world):
    from app.models.organization import Organization
    from app.services.gdpr_erasure_service import erase_org_member_data

    db = _session()
    try:
        org = Organization(
            external_org_id=f"org_erw_{uuid_pkg.uuid4().hex[:8]}", name="erw org", is_active=True
        )
        db.add(org)
        db.commit()
        org_id = int(org.id)
    finally:
        db.close()
    w = world("orgm", org_id=org_id)
    db = _session()
    try:
        db.execute(
            text("INSERT INTO tag (name, user_id, organization_id) VALUES (:n, :u, :o)"),
            {"n": f"erw-{uuid_pkg.uuid4().hex[:6]}", "u": w.user_id, "o": org_id},
        )
        db.commit()
        summary = erase_org_member_data(db, w.user_id, org_id)
    finally:
        db.close()
    try:
        entry, receipt = _receipts(summary["ledger_uuid"])[:2]
        assert receipt.outcome == "verified", (
            {k: v for k, v in receipt.residual.items() if v},
            receipt.unverified,
        )
        assert receipt.deleted["pg.media_file"] == 2
        assert receipt.de_attributed["pg.tag.org"] == 1
        assert receipt.residual["pg.tag.org"] == 0, "still attributed to the subject"
        assert "pg.user" not in receipt.deleted, "an org-member erasure never deletes the account"
        assert _count("SELECT count(*) FROM tag WHERE user_id = :i", i=w.user_id) == 0
        assert entry.subject_type == "org_member"
    finally:
        db = _session()
        try:
            db.execute(text("DELETE FROM tag WHERE organization_id = :o"), {"o": org_id})
            db.execute(
                text("UPDATE media_file SET organization_id = NULL WHERE organization_id = :o"),
                {"o": org_id},
            )
            db.execute(text("DELETE FROM organization WHERE id = :o"), {"o": org_id})
            db.commit()
        finally:
            db.close()
