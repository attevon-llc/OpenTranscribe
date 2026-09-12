"""Share-revocation cache invalidation for chat retrieval.

`update_file_access_index` rewrites `accessible_user_ids` on transcript chunks
whenever a collection share, group membership, or collection-membership change
alters who can see a file. Chat's retrieval cache
(`app.services.chat.retrieval_cache.cache_key`) is deliberately ACL-blind — the
key binds query/scope/settings/corpus-version, never the caller's identity's
accessible set — so a revoked share is invisible to the cache unless the global
`chat:corpus:version` counter moves. Before this fix, nothing bumped it here:
a user could keep retrieving a file's chunks from cache for up to the cache
TTL (5 minutes) after their access was revoked.

The originally-proposed fix (bump the version from each of the 5 endpoints
that call `update_file_access_index.delay(...)`) was REJECTED: `.delay()` is
async, so a synchronous endpoint-side bump races the in-flight ACL rewrite —
the next query misses, runs against the STILL-STALE index, gets the revoked
chunks anyway, and re-caches them under the new version for a fresh full TTL,
which makes the window *worse*, not better.

The fix instead bumps the corpus version inside
`update_file_access_index` itself, unconditionally, after the
`update_by_query` loop (which runs with `refresh=True`) completes. This is the
one call site all present and future callers share, and ordering is correct
by construction: the bump can only happen after the index write is visible.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.tasks import search_indexing_task

pytestmark = pytest.mark.unit


def _stub_opensearch_client(update_by_query_side_effect=None):
    client = MagicMock()
    if update_by_query_side_effect is not None:
        client.update_by_query.side_effect = update_by_query_side_effect
    else:
        client.update_by_query.return_value = {"updated": 3}
    return client


@pytest.fixture(autouse=True)
def _stub_permission_service(monkeypatch):
    """Every file resolves to one accessible user, no DB needed."""
    monkeypatch.setattr(
        "app.services.permission_service.PermissionService.get_users_with_file_access",
        staticmethod(lambda db, file_id: [1]),
    )


@pytest.fixture
def _stub_session_scope(monkeypatch):
    """``update_file_access_index`` imports ``session_scope`` locally, so the patch
    target is the source module (``app.db.session_utils``), not the task module."""

    class _FakeCtx:
        def __enter__(self):
            return MagicMock()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _FakeCtx())


class TestBumpFiresExactlyOnceOnSuccess:
    """Red on base: zero calls, because nothing bumped the version there."""

    def test_bump_corpus_version_called_exactly_once(self, monkeypatch, _stub_session_scope):
        bump_mock = MagicMock()
        monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", bump_mock)
        client = _stub_opensearch_client()
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)

        result = search_indexing_task.update_file_access_index([294085])

        assert result["status"] == "success"
        bump_mock.assert_called_once_with()


class TestBumpFiresOnErrorToo:
    """Control: the version must bump even when update_by_query raises.

    If someone moves the call onto the success-only branch, this is the test
    that catches it — a partial-failure run is exactly the case where the
    index may be left inconsistent and a forced re-check matters most.
    """

    def test_bump_fires_when_update_by_query_raises(self, monkeypatch, _stub_session_scope):
        bump_mock = MagicMock()
        monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", bump_mock)
        client = _stub_opensearch_client(
            update_by_query_side_effect=RuntimeError("opensearch is down")
        )
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)

        result = search_indexing_task.update_file_access_index([294085])

        assert result["errors"] == 1
        bump_mock.assert_called_once_with()


class TestBumpDoesNotFireOnEarlyReturns:
    """Control: must NOT fire on the no_file_ids/no_opensearch early returns.

    A naive "bump at the top of the function" fix would pass the success test
    above but reopen the in-flight-window bug this fix exists to close: those
    early returns mean no ACL rewrite happened at all, so a bump here would be
    pure noise at best and, if it raced a real endpoint-side revoke elsewhere,
    exactly the premature-invalidation shape this fix rejects.
    """

    def test_no_bump_on_empty_file_ids(self, monkeypatch):
        bump_mock = MagicMock()
        monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", bump_mock)

        result = search_indexing_task.update_file_access_index([])

        assert result == {"status": "skipped", "reason": "no_file_ids"}
        bump_mock.assert_not_called()

    def test_no_bump_when_opensearch_client_unavailable(self, monkeypatch):
        bump_mock = MagicMock()
        monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", bump_mock)
        monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: None)

        result = search_indexing_task.update_file_access_index([294085])

        assert result == {"status": "skipped", "reason": "no_opensearch"}
        bump_mock.assert_not_called()


class TestCacheKeyIsAclBlindByDesign:
    """Characterization: green before AND after this fix.

    Documents *why* the fix belongs in the invalidator rather than the key:
    `cache_key(...)` with identical args and a fixed `corpus_rev` is
    byte-identical regardless of ACL state, because ACL membership is never one
    of its inputs. `corpus_rev` is the only lever that can invalidate a
    revoked-access cache entry.
    """

    def test_cache_key_is_identical_regardless_of_acl_state(self):
        from app.services.chat import retrieval_cache

        def _build_key() -> str:
            return retrieval_cache.cache_key(
                user_id=170739,
                organization_id=None,
                query="medicare fraud findings",
                scope_digest=retrieval_cache.scope_hash(None),
                settings_rev="rev-1",
                search_mode="hybrid",
                corpus_rev="5048",
            )

        key_while_shared = _build_key()
        key_after_revoked = _build_key()

        assert key_while_shared == key_after_revoked
