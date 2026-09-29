"""Per-file voiceprint erasure and the orphan-voiceprint sweep, against a real OpenSearch.

Why a real cluster: the defect this module pins down is a property of the engine, not of
our code's control flow. ``purge_media_file`` deletes each speaker embedding **by id**
(realtime) and then proves the deletion with a ``count`` — which reads a **searcher**. A
searcher only stops seeing a deleted document after the next refresh, so a count issued
inside the refresh window reports every just-deleted voiceprint as a survivor. A stand-in
index cannot reproduce that, because it has no searcher.

Observed in the field on a managed single-node OpenSearch 3.x domain: purging 24 files
logged ``N voiceprint doc(s) survive in speakers_v4`` (and the same through the
``speakers`` alias) for 13 of them — every erasure audited as PARTIAL — while a read-only
scan of the same indices minutes later found **zero** speaker documents left. The
documents were gone; the verification could not see that yet. Whether a given purge hit
the window depended only on timing, which is why it was 13 of 24 and not 24 of 24.

The refresh window is made deterministic here by switching the throwaway indices'
``refresh_interval`` to ``-1``: the searcher then stays frozen until something refreshes
it explicitly, which is exactly the state the default 1 s interval is in for the first
second after a delete. Nothing is mocked on the OpenSearch side.

Point the suite at an isolated cluster, never a shared one::

    OPENSEARCH_PORT=<port> pytest backend/tests/integration/test_voiceprint_erasure_opensearch.py
"""

from __future__ import annotations

import datetime
import os
import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("opensearch_speaker_indices"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). The defect is a searcher/refresh "
            "property of the engine and cannot be reproduced against a stand-in."
        ),
    ),
]


def _embedding(dimension: int) -> list[float]:
    return [round((i + 1) * 0.001 % 1.0, 6) for i in range(dimension)]


def _speaker_doc(speaker_uuid: str, dimension: int = PYANNOTE_EMBEDDING_DIMENSION_V4) -> dict:
    now = datetime.datetime.now(datetime.UTC).isoformat()
    return {
        "speaker_id": 1,
        "speaker_uuid": speaker_uuid,
        "user_id": 7,
        "media_file_id": 70,
        "name": "SPEAKER_00",
        "collection_ids": [],
        "segment_count": 3,
        "created_at": now,
        "updated_at": now,
        "embedding": _embedding(dimension),
    }


def _profile_doc(profile_uuid: str) -> dict:
    now = datetime.datetime.now(datetime.UTC).isoformat()
    return {
        "document_type": "profile",
        "profile_id": 1,
        "profile_uuid": profile_uuid,
        "user_id": 7,
        "speaker_count": 1,
        "updated_at": now,
        "embedding": _embedding(PYANNOTE_EMBEDDING_DIMENSION_V4),
    }


@pytest.fixture
def speaker_indices(monkeypatch):
    """Throwaway v3/v4(+alias)/v3_backup speaker indices with auto-refresh switched off."""
    from app.core.config import settings
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v3_backup
    from app.core.constants import get_speaker_index_v4
    from app.services.opensearch_service import client as _client
    from app.services.opensearch_service.indices import _ensure_versioned_speaker_index

    client = _client.opensearch_client
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    monkeypatch.setattr(
        settings, "OPENSEARCH_SPEAKER_INDEX", f"test_vp_erase_{uuid_pkg.uuid4().hex[:10]}"
    )
    v3, v4, backup = get_speaker_index_v3(), get_speaker_index_v4(), get_speaker_index_v3_backup()
    _ensure_versioned_speaker_index(v3, PYANNOTE_EMBEDDING_DIMENSION_V3)
    _ensure_versioned_speaker_index(v4, PYANNOTE_EMBEDDING_DIMENSION_V4)
    _ensure_versioned_speaker_index(backup, PYANNOTE_EMBEDDING_DIMENSION_V3)
    alias = get_speaker_index()
    client.indices.put_alias(index=v4, name=alias)

    def freeze_searcher() -> None:
        client.indices.put_settings(
            index=f"{v3},{v4},{backup}", body={"index": {"refresh_interval": "-1"}}
        )

    try:
        yield SimpleNamespace(
            client=client, v3=v3, v4=v4, backup=backup, alias=alias, freeze=freeze_searcher
        )
    finally:
        for idx in (v3, v4, backup):
            client.indices.delete(index=idx, ignore=[404])


def _put(ns: SimpleNamespace, index: str, doc_id: str, body: dict) -> None:
    ns.client.index(index=index, id=doc_id, body=body)


def _visible(ns: SimpleNamespace, index: str, ids: list[str]) -> int:
    """How many of ``ids`` a freshly refreshed searcher still finds."""
    ns.client.indices.refresh(index=index)
    return int(ns.client.count(index=index, body={"query": {"ids": {"values": ids}}})["count"])


# --------------------------------------------------------------------------- #
# purge_media_file's speaker-embedding step
# --------------------------------------------------------------------------- #


def test_erased_voiceprints_are_not_reported_as_survivors(speaker_indices):
    """The field defect: a clean per-file erasure audited as PARTIAL.

    Before the fix this failed with ``3 voiceprint doc(s) survive in <v4>`` and the same
    through the alias — the exact log lines from the managed cluster — while the
    documents were in fact deleted (the second assertion held either way).
    """
    from app.services.file_cleanup_service import _erase_speaker_docs

    ns = speaker_indices
    uuids = [str(uuid_pkg.uuid4()) for _ in range(3)]
    for u in uuids:
        _put(ns, ns.v4, u, _speaker_doc(u))
    ns.client.indices.refresh(index=ns.v4)
    ns.freeze()  # the moment right after the deletes, inside the refresh window

    failures: list[tuple[str, str]] = []
    _erase_speaker_docs(uuids, lambda stage, err: failures.append((stage, str(err))))

    assert failures == []
    assert _visible(ns, ns.v4, uuids) == 0


def test_a_voiceprint_that_really_survives_is_still_reported(speaker_indices):
    """The fix must not blind the check: a delete the engine refused still audits PARTIAL.

    The refusal is real — a write block on the index — not a patched-out delete.
    """
    from app.services.file_cleanup_service import _erase_speaker_docs

    ns = speaker_indices
    u = str(uuid_pkg.uuid4())
    _put(ns, ns.v4, u, _speaker_doc(u))
    ns.client.indices.refresh(index=ns.v4)
    ns.freeze()
    ns.client.indices.put_settings(index=ns.v4, body={"index": {"blocks.write": True}})

    failures: list[tuple[str, str]] = []
    try:
        _erase_speaker_docs([u], lambda stage, err: failures.append((stage, str(err))))
    finally:
        ns.client.indices.put_settings(index=ns.v4, body={"index": {"blocks.write": False}})

    assert ("speakers", f"1 voiceprint doc(s) survive in {ns.v4}") in failures


def test_the_legacy_v3_backup_copy_is_erased_too(speaker_indices):
    """``speakers_v3_backup`` is re-imported into v3 whenever v3 is found empty.

    The GDPR account/org paths already sweep it; the per-file purge did not, so a
    recording's voiceprint could come back after its file was deleted.
    """
    from app.services.file_cleanup_service import _erase_speaker_docs

    ns = speaker_indices
    u = str(uuid_pkg.uuid4())
    _put(ns, ns.backup, u, _speaker_doc(u, PYANNOTE_EMBEDDING_DIMENSION_V3))
    ns.client.indices.refresh(index=ns.backup)

    failures: list[tuple[str, str]] = []
    _erase_speaker_docs([u], lambda stage, err: failures.append((stage, str(err))))

    assert failures == []
    assert _visible(ns, ns.backup, [u]) == 0


# --------------------------------------------------------------------------- #
# sweep_orphan_voiceprints — cleaning up what earlier erasures left behind
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_state(monkeypatch):
    """Stand in for Postgres: which speaker / profile rows exist.

    The sweep's own DB reads are two single-column queries; the part worth a real
    cluster is everything after them.
    """
    from app.services import voiceprint_orphan_sweep as sweep

    state = SimpleNamespace(speakers=set(), profiles=set())
    monkeypatch.setattr(sweep, "_db_speaker_uuids", lambda: set(state.speakers))
    monkeypatch.setattr(sweep, "_db_profile_uuids", lambda: set(state.profiles))
    return state


def _seed(ns: SimpleNamespace, db_state: SimpleNamespace) -> dict[str, Any]:
    live_spk, dead_spk, dead_v3, dead_bak = (str(uuid_pkg.uuid4()) for _ in range(4))
    live_prof, dead_prof = str(uuid_pkg.uuid4()), str(uuid_pkg.uuid4())
    _put(ns, ns.v4, live_spk, _speaker_doc(live_spk))
    _put(ns, ns.v4, dead_spk, _speaker_doc(dead_spk))
    _put(ns, ns.v3, dead_v3, _speaker_doc(dead_v3, PYANNOTE_EMBEDDING_DIMENSION_V3))
    _put(ns, ns.backup, dead_bak, _speaker_doc(dead_bak, PYANNOTE_EMBEDDING_DIMENSION_V3))
    _put(ns, ns.v4, f"profile_{live_prof}", _profile_doc(live_prof))
    _put(ns, ns.v4, f"profile_{dead_prof}", _profile_doc(dead_prof))
    # A cluster centroid is neither a speaker nor a profile; the sweep must leave it be.
    _put(ns, ns.v4, "cluster_x", {"document_type": "cluster", "user_id": 7})
    for idx in (ns.v3, ns.v4, ns.backup):
        ns.client.indices.refresh(index=idx)
    db_state.speakers = {live_spk}
    db_state.profiles = {live_prof}
    return {
        "live": [live_spk, f"profile_{live_prof}", "cluster_x"],
        "dead": {
            ns.v4: [dead_spk, f"profile_{dead_prof}"],
            ns.v3: [dead_v3],
            ns.backup: [dead_bak],
        },
    }


def test_sweep_is_a_dry_run_by_default_and_counts_every_orphan(speaker_indices, db_state):
    from app.services.voiceprint_orphan_sweep import sweep_orphan_voiceprints

    ns = speaker_indices
    seeded = _seed(ns, db_state)

    report = sweep_orphan_voiceprints()

    assert report["dry_run"] is True
    assert report["orphans_found"] == 4
    assert report["deleted"] == 0
    assert report["indices"][ns.v4]["orphan_speaker_docs"] == 1
    assert report["indices"][ns.v4]["orphan_profile_docs"] == 1
    assert report["indices"][ns.v3]["orphan_speaker_docs"] == 1
    assert report["indices"][ns.backup]["orphan_speaker_docs"] == 1
    for idx, ids in seeded["dead"].items():
        assert _visible(ns, idx, ids) == len(ids), "a dry run deleted something"


def test_sweep_apply_removes_only_orphans_and_is_idempotent(speaker_indices, db_state):
    from app.services.voiceprint_orphan_sweep import sweep_orphan_voiceprints

    ns = speaker_indices
    seeded = _seed(ns, db_state)

    report = sweep_orphan_voiceprints(dry_run=False)

    assert report["deleted"] == 4
    assert report["surviving_orphans"] == 0
    for idx, ids in seeded["dead"].items():
        assert _visible(ns, idx, ids) == 0
    assert _visible(ns, ns.v4, seeded["live"]) == 3

    again = sweep_orphan_voiceprints(dry_run=False)
    assert again["orphans_found"] == 0
    assert again["deleted"] == 0


def test_sweep_refuses_an_empty_database_unless_forced(speaker_indices, db_state):
    """No rows at all looks the same as a lost database; deleting every voiceprint on
    that guess is unrecoverable, so it takes an explicit ``force``."""
    from app.services.voiceprint_orphan_sweep import sweep_orphan_voiceprints

    ns = speaker_indices
    seeded = _seed(ns, db_state)
    db_state.speakers, db_state.profiles = set(), set()

    refused = sweep_orphan_voiceprints(dry_run=False)
    assert refused["refused"] == "empty_database"
    assert refused["deleted"] == 0
    assert _visible(ns, ns.v4, seeded["dead"][ns.v4]) == 2

    forced = sweep_orphan_voiceprints(dry_run=False, force=True)
    assert forced["refused"] is None
    assert forced["deleted"] == 6  # every speaker + profile doc; the cluster doc stays
    assert _visible(ns, ns.v4, ["cluster_x"]) == 1


def test_profile_left_without_speakers_loses_its_voiceprint(speaker_indices):
    """The averaged profile embedding is keyed ``profile_<uuid>``; clearing it by the
    integer id matched nothing and left the voiceprint indexed and matchable."""
    from app.services.profile_embedding_service import _process_profile_with_no_speakers

    ns = speaker_indices
    profile_uuid = str(uuid_pkg.uuid4())
    doc_id = f"profile_{profile_uuid}"
    _put(ns, ns.v4, doc_id, _profile_doc(profile_uuid))
    ns.client.indices.refresh(index=ns.v4)
    profile: Any = SimpleNamespace(
        id=1, uuid=uuid_pkg.UUID(profile_uuid), embedding_count=2, last_embedding_update=None
    )

    _process_profile_with_no_speakers(profile, 1)

    assert _visible(ns, ns.v4, [doc_id]) == 0
