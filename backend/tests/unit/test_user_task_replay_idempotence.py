"""Why each user-triggered task is, or is not, on the replay allowlist (issue #1159).

A replayed task runs a second time, possibly after a first run that got most of the way
through. For the allowlisted search-index tasks that must change nothing: the second run sends
the same writes, derived from Postgres and the arguments alone, with no counter, append or
accumulate in the script. For ``process_speaker_merge_background`` the opposite is pinned: its
voiceprint average is not a fixed point, which is why it is an exclusion. If that step is ever
made idempotent, the last test fails and the exclusion should be revisited.

These use a recording OpenSearch client; a live double-run against a real index is a separate,
stack-bound check.
"""

from __future__ import annotations

import contextlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import task_replay as tr

#: Painless constructs that make a script depend on the document's previous value.
_ACCUMULATING = re.compile(r"\+=|\+\+|\.add\(|\.addAll\(|\.append\(|\*=")


class RecordingClient:
    """Records every ``update_by_query`` and answers with a fixed response."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.indices = SimpleNamespace(refresh=lambda **_kw: None)

    def update_by_query(self, *, index: str, body: dict[str, Any], **kwargs: Any) -> dict:
        self.bodies.append({"index": index, "body": body, "kwargs": kwargs})
        return {"updated": 1, "version_conflicts": 0}


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def join(self, *_a: Any, **_k: Any) -> _Rows:
        return self

    def filter(self, *_a: Any, **_k: Any) -> _Rows:
        return self

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeDb:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def query(self, *_a: Any) -> _Rows:
        return _Rows(self._rows)


def _scope(rows: list[tuple[Any, ...]]):
    @contextlib.contextmanager
    def session_scope():
        yield FakeDb(rows)

    return session_scope


@pytest.fixture
def client(monkeypatch) -> RecordingClient:
    fake = RecordingClient()
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", lambda: None)
    return fake


def _assert_second_run_repeats_the_first(
    client: RecordingClient, first: dict, second: dict, *, check_script: bool = True
):
    assert client.bodies, "the task issued no write, so the comparison below proves nothing"
    half = len(client.bodies) // 2
    assert len(client.bodies) == 2 * half
    assert client.bodies[:half] == client.bodies[half:], "a replay wrote something different"
    assert first == second
    for call in client.bodies if check_script else []:
        source = call["body"]["script"]["source"]
        assert not _ACCUMULATING.search(source), f"script depends on the previous value: {source}"


def test_access_index_update_is_a_pure_overwrite(client, monkeypatch):
    from app.services.permission_service import PermissionService
    from app.tasks.search_file_field_tasks import update_file_access_index

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope([(3,)]))
    monkeypatch.setattr(
        PermissionService, "get_users_with_file_access", staticmethod(lambda _db, _fid: [1, 2])
    )

    first = update_file_access_index.run([7])
    second = update_file_access_index.run([7])

    _assert_second_run_repeats_the_first(client, first, second)
    assert first["status"] == "success"
    assert client.bodies[0]["body"]["script"]["params"] == {"ids": [1, 2], "cids": [3]}


def test_tag_index_update_is_a_pure_overwrite(client, monkeypatch):
    from app.tasks.search_file_field_tasks import update_file_tags_index

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope([(7, "alpha"), (7, "beta")]))

    first = update_file_tags_index.run([7])
    second = update_file_tags_index.run([7])

    _assert_second_run_repeats_the_first(client, first, second)
    assert client.bodies[0]["body"]["script"]["params"] == {"tags": ["alpha", "beta"]}


def _patch_rename_context(monkeypatch, *, current_name=None, current_title=None):
    from app.tasks import rename_propagation_task as rp

    monkeypatch.setattr(rp, "_current_speaker_name", lambda _sid: current_name)
    monkeypatch.setattr(rp, "_current_file_title", lambda _uuid: current_title)
    monkeypatch.setattr(rp, "_file_if_voiceprints_off", lambda _uuid: None)
    monkeypatch.setattr(rp, "_finish", lambda *_a, **_k: None)
    monkeypatch.setattr(rp, "_retry_on_conflicts", lambda *_a, **_k: False)
    return rp


def test_speaker_rename_replay_rewrites_the_same_documents(client, monkeypatch):
    rp = _patch_rename_context(monkeypatch, current_name="Alice")

    first = rp.propagate_speaker_rename.run("f-uuid", ["SPEAKER_00"], "Alice", 5)
    second = rp.propagate_speaker_rename.run("f-uuid", ["SPEAKER_00"], "Alice", 5)

    # The roster is rebuilt into a fresh list from the stored one and written only when it
    # differs, so it is not an accumulator even though it calls ``add`` on that new list.
    _assert_second_run_repeats_the_first(client, first, second, check_script=False)
    source = client.bodies[0]["body"]["script"]["source"]
    assert "new ArrayList()" in source and "rebuilt.equals(ctx._source.speakers)" in source
    # Only documents that still carry a stale name are selected, so after a finished run
    # the same request matches nothing: the replay of a completed rename is a no-op.
    assert client.bodies[0]["body"]["script"]["params"]["old_names"] == ["SPEAKER_00"]


def test_title_rename_replay_rewrites_the_same_documents(client, monkeypatch):
    rp = _patch_rename_context(monkeypatch, current_title="Q3 review")

    first = rp.propagate_title_rename.run("f-uuid", "Q3 review")
    second = rp.propagate_title_rename.run("f-uuid", "Q3 review")

    _assert_second_run_repeats_the_first(client, first, second)
    assert client.bodies[0]["body"]["script"]["params"] == {"title": "Q3 review"}


def test_a_replayed_rename_writes_the_name_current_at_replay_time(client, monkeypatch):
    """The replay carries the dispatched name; a rename since then must win, not be undone."""
    rp = _patch_rename_context(monkeypatch, current_title="Newer title")

    rp.propagate_title_rename.run("f-uuid", "Older title")

    assert client.bodies[0]["body"]["script"]["params"] == {"title": "Newer title"}


def test_digest_regeneration_replay_issues_the_same_index_calls(monkeypatch):
    from app.services.search import indexing_service as idx
    from app.tasks import search_indexing_task
    from app.tasks.rename_digest_task import regenerate_rename_digests

    calls: list[tuple[str, int, str]] = []

    class FakeIndexer:
        def _index_digest_plane(self, *, file_id, file_uuid, **_kw):
            calls.append(("digest", file_id, file_uuid))

        def _index_summary_plane(self, *, file_id, file_uuid, **_kw):
            calls.append(("summary", file_id, file_uuid))

    meta: dict[str, Any] = {
        "title": "t",
        "tag_names": [],
        "upload_time": None,
        "language": "en",
        "content_type": "audio/wav",
        "duration": 1.0,
        "file_size": 1,
        "collection_ids": [],
        "accessible_user_ids": [],
    }

    class _Db:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def first(self):
            return SimpleNamespace(id=9, user_id=4)

    @contextlib.contextmanager
    def scope():
        yield _Db()

    stubs = {
        (idx, "TranscriptIndexingService"): FakeIndexer,
        ("app.services.search.pipelines", "is_neural_pipeline_available"): lambda: False,
        ("app.db.session_utils", "session_scope"): scope,
        (search_indexing_task, "extract_file_index_metadata"): lambda *_a: meta,
        ("app.services.takedown_service", "filter_suppressed_file_uuids"): lambda f, _u: f,
        ("app.utils.websocket_notify", "send_ws_event_for_file"): lambda *_a, **_k: None,
    }
    for (target, attr), stub in stubs.items():
        if isinstance(target, str):
            monkeypatch.setattr(f"{target}.{attr}", stub)
        else:
            monkeypatch.setattr(target, attr, stub)

    first = regenerate_rename_digests.run(["u-1"], "Alice", 5)
    n = len(calls)
    second = regenerate_rename_digests.run(["u-1"], "Alice", 5)

    assert n == 2, "the first run rebuilt neither plane"
    assert calls[:n] == calls[n:], "a replay rebuilt something else"
    assert first == second


def test_every_replayable_user_task_is_pinned_by_a_test_here():
    pinned = {
        "update_file_access_index",
        "update_file_tags_index",
        "propagate_speaker_rename",
        "propagate_title_rename",
        "regenerate_rename_digests",
    }
    assert pinned <= tr.REPLAYABLE_TASKS


def test_the_speaker_merge_voiceprint_average_is_not_a_fixed_point(monkeypatch):
    """The reason ``process_speaker_merge_background`` is excluded, not replayed.

    The source voiceprint stays in the index until a later step deletes it, so a run lost in
    between and replayed averages the source in again and drags the survivor towards it.
    """
    from app.api.endpoints import speakers_merge

    store = {"source": [1.0, 0.0], "target": [0.0, 1.0]}

    monkeypatch.setattr("app.services.opensearch_service.get_speaker_embedding", store.get)
    monkeypatch.setattr(
        "app.services.opensearch_service.add_speaker_embedding",
        lambda *, speaker_uuid, embedding, **_kw: store.__setitem__(speaker_uuid, embedding),
    )
    target = {
        "uuid": "target",
        "id": 2,
        "user_id": 1,
        "name": "n",
        "profile_id": None,
        "media_file_id": None,
        "display_name": None,
    }

    speakers_merge._merge_speaker_embeddings("source", target)
    after_first = list(store["target"])
    speakers_merge._merge_speaker_embeddings("source", target)

    assert after_first == [0.5, 0.5]
    assert store["target"] != after_first
