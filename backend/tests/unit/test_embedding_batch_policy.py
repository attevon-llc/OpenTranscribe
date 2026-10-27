"""The admin embedding batch jobs under the privacy policy (#1280, plan D5 and Q3).

A repair, migration or normalization run must write no voiceprint of any kind for a scope
that has voiceprints off. For the v4 migration that means the scope has none in the index the
alias is swapped to, so finalize reports it (``skipped_by_policy``) and does not read the
missing documents as damage. Every case has its control with the layer on.

The tasks open their own sessions (``session_scope``); the tests point those at the savepoint
session so the rows they just built are visible to the policy.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np
import pytest

from app.models.media import SpeakerProfile
from app.tasks import embedding_consistency_repair as repair
from app.tasks import embedding_migration_v4 as mig
from app.tasks import speaker_embedding_migration as normalize
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import note
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _bridge(db_session):
    @contextlib.contextmanager
    def scope():
        yield db_session

    return scope


@pytest.fixture
def bridged(monkeypatch, db_session):
    scope = _bridge(db_session)
    for module in (repair, mig, normalize):
        # `raising=False`: a tree without the gate has no `session_scope` here, and the
        # red-check of these tests must fail on the ASSERTION (a document was written), not
        # on this fixture.
        monkeypatch.setattr(module, "session_scope", scope, raising=False)
    return db_session


def _prepared(user, media_file, speaker) -> Any:
    return SimpleNamespace(
        speakers=[
            SimpleNamespace(
                id=speaker.id, uuid=str(speaker.uuid), name=speaker.name, profile_id=None
            )
        ],
        extra={},
        user_id=user.id,
        media_file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        organization_id=media_file.organization_id,
    )


def _results(speaker) -> dict[str, list[Any]]:
    return {"embedding": [SimpleNamespace(speaker_id=speaker.id, value=np.array([3.0, 4.0]))]}


# ---------------------------------------------------------------------------
# Consistency repair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("writer", ["_v3_result_writer", "_v4_result_writer"])
def test_the_consistency_repair_recreates_a_missing_document_only_when_allowed(
    bridged, normal_user, monkeypatch, allowed, writer
):
    org = make_org(bridged, voiceprints=allowed)
    media_file = file_in(bridged, normal_user, org)
    speaker = make_speaker(bridged, normal_user, media_file)
    written: list[str] = []
    for name in ("add_speaker_embedding", "add_speaker_embedding_v4"):
        monkeypatch.setattr(
            f"app.services.opensearch_service.{name}",
            lambda **kwargs: note(written, kwargs["speaker_uuid"], {"result": "created"}),
        )

    count = getattr(repair, writer)(
        _prepared(normal_user, media_file, speaker), _results(speaker), {str(speaker.uuid)}
    )

    assert count == (1 if allowed else 0)
    assert written == ([str(speaker.uuid)] if allowed else [])


def test_the_repair_batch_counts_a_policy_skipped_file_as_done_not_failed(
    bridged, normal_user, monkeypatch
):
    org = make_org(bridged, voiceprints=False)
    media_file = file_in(bridged, normal_user, org)
    speaker = make_speaker(bridged, normal_user, media_file)
    progress: list[dict[str, Any]] = []
    monkeypatch.setattr(
        repair,
        "_update_repair_progress",
        lambda total, **kwargs: progress.append(kwargs),
    )
    monkeypatch.setattr(repair, "_check_repair_completion", lambda *a, **k: None)
    monkeypatch.setattr(repair, "get_redis", lambda: MagicMock(get=lambda k: None))
    prepared = _prepared(normal_user, media_file, speaker)
    runs: list[int] = []
    monkeypatch.setattr(repair, "_run_repair_phase", lambda *a, **k: note(runs, 1, 0))

    with patch("app.tasks.migration_pipeline.prepare_file", return_value=prepared):
        result = repair.speaker_embedding_consistency_repair_batch_task.run(
            [str(media_file.uuid)],
            total_files=1,
            missing_v3_uuids=[str(speaker.uuid)],
        )

    assert result["status"] == "no_files"
    assert runs == []
    assert progress == [{"success": True, "user_id": 1, "skipped_by_policy": 1}]


# ---------------------------------------------------------------------------
# v4 migration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_the_v4_migration_writes_a_files_documents_only_when_allowed(
    bridged, normal_user, monkeypatch, allowed
):
    org = make_org(bridged, voiceprints=allowed)
    media_file = file_in(bridged, normal_user, org)
    speaker = make_speaker(bridged, normal_user, media_file)
    bulk: list[int] = []
    skipped: list[int] = []
    monkeypatch.setattr(
        mig, "_bulk_write_v4_embeddings", lambda docs: note(bulk, len(docs), len(docs))
    )
    monkeypatch.setattr(mig, "_count_skipped_by_policy", lambda: skipped.append(1))

    written = mig._embedding_result_writer(
        _prepared(normal_user, media_file, speaker), _results(speaker)
    )

    assert written == (1 if allowed else 0)
    assert bulk == ([1] if allowed else [])
    assert skipped == ([] if allowed else [1])


@pytest.mark.parametrize("allowed", [True, False])
def test_the_v4_profile_backfill_writes_a_profile_only_when_its_scope_is_allowed(
    bridged, normal_user, monkeypatch, allowed
):
    org = make_org(bridged, voiceprints=allowed)
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="P")
    profile.organization_id = org.id
    bridged.add(profile)
    bridged.flush()
    client = MagicMock()
    client.search.return_value = {
        "_scroll_id": "s",
        "hits": {
            "hits": [
                {
                    "_source": {
                        "profile_id": profile.id,
                        "profile_uuid": str(profile.uuid),
                        "user_id": normal_user.id,
                        "organization_id": org.id,
                        "segment_count": 1,
                        "embedding": [1.0, 0.0],
                    }
                }
            ]
        },
    }
    client.scroll.return_value = {"hits": {"hits": []}}
    stored: list[str] = []
    monkeypatch.setattr(
        "app.services.opensearch_service.store_profile_embedding_v4",
        lambda **kwargs: note(stored, kwargs["profile_uuid"], True),
    )

    written = mig.migrate_profile_documents_to_v4(client, "speakers_v4")

    assert written == (1 if allowed else 0)
    assert stored == ([str(profile.uuid)] if allowed else [])


def test_the_scopes_off_by_policy_are_found_from_files_and_profiles(bridged, normal_user):
    off_org = make_org(bridged, voiceprints=False)
    on_org = make_org(bridged)
    file_in(bridged, normal_user, off_org)
    file_in(bridged, normal_user, on_org)
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="P")
    profile.organization_id = off_org.id
    bridged.add(profile)
    bridged.flush()

    off = mig._scopes_off_by_policy()

    assert (normal_user.id, off_org.id) in off
    assert (normal_user.id, on_org.id) not in off


def test_the_exclusion_clauses_name_each_off_organization_once():
    clauses = mig._off_scope_exclusion([(1, 7), (2, 7), (3, None)])

    assert len(clauses) == 2
    by_users = {tuple(c["bool"]["filter"][0]["terms"]["user_id"]): c for c in clauses}
    assert set(by_users) == {(1, 2), (3,)}
    # The personal scope is "no organization field", not "organization zero".
    personal = by_users[(3,)]["bool"]["filter"][1:]
    assert any(
        "must_not" in str(clause) and "organization_id" in str(clause) for clause in personal
    )


def _finalize(skipped_files: int, off_scopes: list[tuple[int, int | None]]):
    """Run finalize against a fake index and return (result, count calls, profile counts)."""
    client = MagicMock()
    client.indices.exists.return_value = True
    count_bodies: list[Any] = []

    def count(index, body=None):
        count_bodies.append(body)
        return {"count": 10}

    client.count.side_effect = count
    profile_calls: list[dict[str, Any]] = []

    def count_profiles(client_arg, index, **kwargs):
        profile_calls.append({"index": index, **kwargs})
        return 2

    attrs: dict[str, Any] = {
        "get_speaker_index_v4": lambda: "speakers_v4",
        "get_speaker_index": lambda: "speakers",
        "migrate_profile_documents_to_v4": lambda c, i: 0,
        "_count_profile_docs": count_profiles,
        "_scopes_off_by_policy": lambda: off_scopes,
        "_take_skipped_by_policy": lambda: skipped_files,
        "send_ws_event": lambda *a, **k: None,
    }
    service_attrs: dict[str, Any] = {
        "get_opensearch_client": lambda: client,
        "get_active_versioned_index": lambda: "speakers_v3",
        "swap_speaker_alias": lambda target: {"status": "success"},
    }
    with (
        patch.multiple("app.services.opensearch_service", **service_attrs),
        patch.multiple(mig, **attrs),
        patch.object(mig.EmbeddingModeService, "clear_cache"),
        patch.object(mig.migration_progress, "clear_status"),
        patch("app.services.migration_lock_service.migration_lock.deactivate"),
    ):
        result = mig.finalize_v4_migration_task.run(user_id=1)
    return result, count_bodies, profile_calls


def test_finalize_reports_what_it_skipped_and_leaves_those_scopes_out_of_the_guards():
    result, count_bodies, profile_calls = _finalize(skipped_files=3, off_scopes=[(5, None)])

    assert result["status"] == "success"
    assert result["skipped_by_policy"] == {"scopes": 1, "files": 3}
    # The v3 reference counts leave the skipped scope out, so it is not read as a loss.
    v3_speaker_count = next(b for b in count_bodies if b)
    assert v3_speaker_count["query"]["bool"]["must_not"]
    v3_profiles = next(c for c in profile_calls if c["index"] == "speakers_v3")
    assert v3_profiles["exclude"]
    assert not next(c for c in profile_calls if c["index"] == "speakers_v4").get("exclude")


def test_finalize_with_nothing_skipped_uses_the_unfiltered_reference_counts():
    result, count_bodies, profile_calls = _finalize(skipped_files=0, off_scopes=[])

    assert result["skipped_by_policy"] == {"scopes": 0, "files": 0}
    assert all(not b for b in count_bodies)
    assert not any(c.get("exclude") for c in profile_calls)


def test_finalize_refuses_to_swap_when_the_policy_cannot_be_read():
    def unreadable() -> list[tuple[int, int | None]]:
        raise RuntimeError("database unreachable")

    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.return_value = {"count": 10}
    swaps: list[str] = []
    with (
        patch.multiple(
            "app.services.opensearch_service",
            get_opensearch_client=lambda: client,
            get_active_versioned_index=lambda: "speakers_v3",
            swap_speaker_alias=lambda target: note(swaps, target, {"status": "success"}),
        ),
        patch.multiple(
            mig,
            get_speaker_index_v4=lambda: "speakers_v4",
            get_speaker_index=lambda: "speakers",
            _scopes_off_by_policy=unreadable,
            send_ws_event=lambda *a, **k: None,
        ),
        patch.object(mig.EmbeddingModeService, "clear_cache"),
        patch.object(mig.migration_progress, "clear_status"),
        patch("app.services.migration_lock_service.migration_lock.deactivate"),
    ):
        result = mig.finalize_v4_migration_task.run(user_id=1)

    assert result["status"] == "error"
    assert swaps == []


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _hit(doc_id: str, user_id: int | None, org_id: int | None) -> dict[str, Any]:
    source: dict[str, Any] = {"embedding": [10.0, 0.0], "organization_id": org_id}
    if user_id is not None:
        source["user_id"] = user_id
    return {"_id": doc_id, "_source": source}


@pytest.mark.parametrize("allowed", [True, False])
def test_the_normalizer_rewrites_a_vector_only_when_its_scope_is_allowed(
    bridged, normal_user, allowed
):
    org = make_org(bridged, voiceprints=allowed)
    summary: dict[str, Any] = {"total_found": 0, "already_normalized": 0}

    ids, vectors = normalize._process_hits_batch(
        [_hit("doc", normal_user.id, org.id)], summary, normalize._ScopeGate()
    )

    assert ids == (["doc"] if allowed else [])
    assert summary.get("skipped_by_policy", 0) == (0 if allowed else 1)


def test_the_normalizer_leaves_a_document_with_no_owner_alone(bridged):
    summary: dict[str, Any] = {"total_found": 0, "already_normalized": 0}

    ids, _ = normalize._process_hits_batch(
        [_hit("orphan", None, None)], summary, normalize._ScopeGate()
    )

    assert ids == []
    assert summary["skipped_by_policy"] == 1
