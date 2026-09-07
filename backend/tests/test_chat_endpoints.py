"""API-level tests for the chat endpoints (issue #52).

These exercise the surface a browser actually talks to: authorization, tenancy,
input validation, the SSE stream, the abuse limits, and the capability gate.

The LLM and OpenSearch are stubbed — this suite is about the HTTP contract and
the ownership boundary, not about answer quality. Everything else (the database,
auth, routing, serialization) is real, because the defects this suite exists to
catch live precisely in the seams between those.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.chat import ChatConversation
from app.services.llm_stream import LLMStreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create(client, headers, **body) -> dict:
    response = client.post("/api/chat/conversations", json=body or {}, headers=headers)
    assert response.status_code == 201, response.text
    created: dict = response.json()
    return created


class _FakeLLM:
    """Stands in for LLMService, scripted with the events a provider would send."""

    def __init__(self, events=None, provider="openai", model="test-model"):
        self.config = SimpleNamespace(
            provider=SimpleNamespace(value=provider), model=model, temperature=0.3
        )
        self.user_context_window = 8192
        self.response_tokens = 1000
        self._events = events or [
            LLMStreamEvent(type="delta", text="The team agreed to ship on Tuesday."),
            LLMStreamEvent(type="usage", prompt_tokens=100, completion_tokens=20),
            LLMStreamEvent(type="done", finish_reason="stop"),
        ]

    def chat_completion_stream(self, messages, cancel_event=None, **kwargs):
        yield from self._events

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.fixture
def stub_llm(db_session):
    """Patch the LLM factory and retrieval so a send completes without services.

    Also bridges ``session_scope`` to the test session. The streaming service
    deliberately opens its OWN session (it outlives the request's dependency
    scope, and a stream still writing when that session closes would fail exactly
    when it needs to persist the partial answer). Under the savepoint harness a
    genuinely separate connection cannot see rows this test has not committed to
    the outer transaction, so persistence would fail on a foreign key that is
    perfectly valid in production.
    """

    @contextmanager
    def _test_session_scope():
        # Commit so the streaming path's writes are visible to later assertions;
        # never close — the fixture owns this session's lifetime.
        yield db_session
        db_session.commit()

    llm = _FakeLLM()
    with (
        patch("app.services.llm_service.LLMService.create_from_settings", return_value=llm),
        patch("app.services.llm_service.LLMService.create_from_config_id", return_value=llm),
        patch("app.services.chat.retrieval.retrieve_chunks", return_value=[]),
        patch("app.db.session_utils.session_scope", _test_session_scope),
    ):
        yield llm


def _read_sse(response) -> list[tuple[str, str]]:
    """Parse a streamed SSE body into (event, data) pairs."""
    frames: list[tuple[str, str]] = []
    event = None
    for line in response.text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            frames.append((event, line[5:].strip()))
            event = None
    return frames


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_every_chat_route_requires_authentication(client):
    for method, path in (
        ("get", "/api/chat/conversations"),
        ("post", "/api/chat/conversations"),
        ("get", f"/api/chat/conversations/{uuid_pkg.uuid4()}"),
        ("post", "/api/chat/context/estimate"),
        ("get", "/api/user-settings/chat"),
    ):
        kwargs: dict = {"json": {}} if method == "post" else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------


def test_create_returns_a_conversation_with_defaults(client, auth_headers):
    body = _create(client, auth_headers)

    assert body["uuid"]
    assert body["is_archived"] is False
    assert body["message_count"] == 0
    assert body["scope"] == {
        "file_uuids": [],
        "collection_uuids": [],
        "tag_names": [],
        "speakers": [],
    }


def test_create_accepts_a_scope_and_settings(client, auth_headers):
    body = _create(
        client,
        auth_headers,
        title="Scoped",
        scope={"file_uuids": [], "collection_uuids": [], "tag_names": ["q3"], "speakers": ["Dana"]},
        settings={"use_context": False, "temperature": 0.7},
    )

    assert body["title"] == "Scoped"
    assert body["scope"]["tag_names"] == ["q3"]
    assert body["scope"]["speakers"] == ["Dana"]
    assert body["use_context"] is False


def test_list_returns_only_the_callers_conversations(client, auth_headers, other_user_auth_headers):
    mine = _create(client, auth_headers, title="Mine")
    _create(client, other_user_auth_headers, title="Theirs")

    body = client.get("/api/chat/conversations", headers=auth_headers).json()
    uuids = [c["uuid"] for c in body["conversations"]]

    assert mine["uuid"] in uuids
    assert all(c["title"] != "Theirs" for c in body["conversations"])


def test_list_can_search_by_title(client, auth_headers):
    _create(client, auth_headers, title="Budget planning call")
    _create(client, auth_headers, title="Unrelated standup")

    body = client.get(
        "/api/chat/conversations", params={"q": "budget"}, headers=auth_headers
    ).json()

    assert [c["title"] for c in body["conversations"]] == ["Budget planning call"]


def test_list_sorts_a_never_messaged_conversation_ahead_of_an_old_one(
    client, auth_headers, db_session
):
    """A conversation with no last_message_at must not be sorted to the end.

    Regression for the sidebar bug: a brand-new conversation (last_message_at
    NULL) was sorted with nullslast() -- always after every messaged
    conversation, no matter how stale -- so it never landed within the first
    page the sidebar loads.
    """
    old = _create(client, auth_headers, title="Old")
    old_row = db_session.query(ChatConversation).filter_by(uuid=old["uuid"]).one()
    old_row.last_message_at = datetime.now(UTC) - timedelta(days=30)
    db_session.commit()

    new = _create(client, auth_headers, title="Brand new")

    body = client.get("/api/chat/conversations", headers=auth_headers).json()
    uuids = [c["uuid"] for c in body["conversations"]]

    assert uuids[0] == new["uuid"]
    assert old["uuid"] in uuids


def test_list_paging_includes_a_never_messaged_conversation_on_page_one(
    client, auth_headers, db_session
):
    """limit=1 must return the never-messaged conversation, not just the sort order.

    This is what the sidebar actually trips over: the comparator can be
    correct in isolation while the offset/limit page it feeds still excludes
    the new conversation if the underlying query disagrees.
    """
    old = _create(client, auth_headers, title="Old")
    old_row = db_session.query(ChatConversation).filter_by(uuid=old["uuid"]).one()
    old_row.last_message_at = datetime.now(UTC) - timedelta(days=30)
    db_session.commit()

    new = _create(client, auth_headers, title="Brand new")

    body = client.get("/api/chat/conversations", params={"limit": 1}, headers=auth_headers).json()

    assert [c["uuid"] for c in body["conversations"]] == [new["uuid"]]


def test_archived_conversations_are_a_separate_list(client, auth_headers):
    conversation = _create(client, auth_headers, title="To archive")
    client.patch(
        f"/api/chat/conversations/{conversation['uuid']}",
        json={"is_archived": True},
        headers=auth_headers,
    )

    active = client.get("/api/chat/conversations", headers=auth_headers).json()
    archived = client.get(
        "/api/chat/conversations", params={"archived": True}, headers=auth_headers
    ).json()

    assert conversation["uuid"] not in [c["uuid"] for c in active["conversations"]]
    assert conversation["uuid"] in [c["uuid"] for c in archived["conversations"]]


def test_patch_merges_settings_rather_than_replacing_them(client, auth_headers):
    conversation = _create(client, auth_headers, settings={"temperature": 0.9})

    client.patch(
        f"/api/chat/conversations/{conversation['uuid']}",
        json={"settings": {"search_mode": "keyword"}},
        headers=auth_headers,
    )
    body = client.get(
        f"/api/chat/conversations/{conversation['uuid']}", headers=auth_headers
    ).json()

    # Setting one control from the panel must not clear the others.
    assert body["settings"]["temperature"] == 0.9
    assert body["settings"]["search_mode"] == "keyword"


def test_delete_removes_the_conversation(client, auth_headers):
    conversation = _create(client, auth_headers)

    assert (
        client.delete(
            f"/api/chat/conversations/{conversation['uuid']}", headers=auth_headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/chat/conversations/{conversation['uuid']}", headers=auth_headers
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Ownership — the authorization boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("get", "", None),
        ("patch", "", {"title": "hijacked"}),
        ("delete", "", None),
        ("get", "/messages", None),
        ("get", "/export", None),
    ],
)
def test_another_users_conversation_is_404_not_403(
    client, auth_headers, other_user_auth_headers, method, suffix, payload
):
    """404, never 403: a probe must not confirm the conversation exists."""
    theirs = _create(client, other_user_auth_headers, title="Private")

    kwargs: dict = {"headers": auth_headers}
    if payload is not None:
        kwargs["json"] = payload
    response = getattr(client, method)(
        f"/api/chat/conversations/{theirs['uuid']}{suffix}", **kwargs
    )

    assert response.status_code == 404


def test_another_users_conversation_cannot_be_written_to(
    client, auth_headers, other_user_auth_headers, stub_llm
):
    theirs = _create(client, other_user_auth_headers)

    response = client.post(
        f"/api/chat/conversations/{theirs['uuid']}/messages",
        json={"content": "leak it"},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_a_deleted_conversation_stays_gone(client, auth_headers):
    conversation = _create(client, auth_headers)
    client.delete(f"/api/chat/conversations/{conversation['uuid']}", headers=auth_headers)

    assert (
        client.patch(
            f"/api/chat/conversations/{conversation['uuid']}",
            json={"title": "back"},
            headers=auth_headers,
        ).status_code
        == 404
    )


def test_cancel_rejects_a_message_the_caller_does_not_own(
    client, auth_headers, other_user_auth_headers, stub_llm
):
    """Regression: cancel had no ownership check and no uuid validation."""
    theirs = _create(client, other_user_auth_headers)
    with client.stream(
        "POST",
        f"/api/chat/conversations/{theirs['uuid']}/messages",
        json={"content": "hello"},
        headers=other_user_auth_headers,
    ) as stream:
        frames = _read_sse(SimpleNamespace(text=b"".join(stream.iter_bytes()).decode()))

    start = next(data for event, data in frames if event == "start")
    their_message_uuid = __import__("json").loads(start)["assistant_message_uuid"]

    response = client.post(f"/api/chat/messages/{their_message_uuid}/cancel", headers=auth_headers)
    assert response.status_code == 404


def test_cancel_rejects_a_non_uuid_path_segment(client, auth_headers):
    """An unvalidated segment became a Redis key — arbitrary keyspace writes."""
    response = client.post("/api/chat/messages/not-a-uuid/cancel", headers=auth_headers)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_message_is_rejected(client, auth_headers):
    conversation = _create(client, auth_headers)
    response = client.post(
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": ""},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_oversized_message_is_rejected(client, auth_headers):
    conversation = _create(client, auth_headers)
    response = client.post(
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "x" * 8001},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_scope_list_caps_are_enforced(client, auth_headers):
    """These bound the Postgres resolution and the OpenSearch terms filter."""
    too_many_files = [str(uuid_pkg.uuid4()) for _ in range(101)]
    response = client.post(
        "/api/chat/conversations",
        json={"scope": {"file_uuids": too_many_files}},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_malformed_uuids_in_scope_are_rejected(client, auth_headers):
    """Validated before they can reach SQL or OpenSearch."""
    response = client.post(
        "/api/chat/conversations",
        json={"scope": {"file_uuids": ["'; DROP TABLE media_file; --"]}},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_invalid_search_mode_is_rejected(client, auth_headers):
    conversation = _create(client, auth_headers)
    response = client.post(
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "hi", "search_mode": "telepathy"},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_temperature_is_range_checked(client, auth_headers):
    response = client.post(
        "/api/chat/conversations",
        json={"settings": {"temperature": 5.0}},
        headers=auth_headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The SSE stream
# ---------------------------------------------------------------------------


def test_send_streams_the_contract_frames_and_persists(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)

    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "What did the team decide?"},
        headers=auth_headers,
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        # Buffering would defeat the whole point of streaming.
        assert stream.headers.get("x-accel-buffering") == "no"
        body = b"".join(stream.iter_bytes()).decode()

    frames = _read_sse(SimpleNamespace(text=body))
    events = [event for event, _ in frames]

    assert events[0] == "start"
    assert "delta" in events
    assert events[-1] == "done"

    # Both sides of the exchange are persisted, in order.
    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "What did the team decide?"
    assert "Tuesday" in messages[1]["content"]
    assert messages[1]["status"] == "complete"


def test_reasoning_streams_separately_and_persists_across_a_reload(
    client, auth_headers, db_session
):
    """Full round trip: send → stream → persist → reload → reasoning still visible.

    Reasoning must arrive as its own SSE frame (never mixed into ``delta``/the
    final answer), and reloading the conversation afterwards must return it on
    its own field too — the collapsible UI block has nothing to render from a
    field that only existed during the live stream.
    """
    llm = _FakeLLM(
        events=[
            LLMStreamEvent(type="reasoning", text="Let me check the transcript. "),
            LLMStreamEvent(type="reasoning", text="Tuesday looks right."),
            LLMStreamEvent(type="delta", text="They agreed to ship on Tuesday."),
            LLMStreamEvent(type="usage", prompt_tokens=100, completion_tokens=20),
            LLMStreamEvent(type="done", finish_reason="stop"),
        ]
    )

    @contextmanager
    def _test_session_scope():
        yield db_session
        db_session.commit()

    with (
        patch("app.services.llm_service.LLMService.create_from_settings", return_value=llm),
        patch("app.services.llm_service.LLMService.create_from_config_id", return_value=llm),
        patch("app.services.chat.retrieval.retrieve_chunks", return_value=[]),
        patch("app.db.session_utils.session_scope", _test_session_scope),
    ):
        conversation = _create(client, auth_headers)

        with client.stream(
            "POST",
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "When do we ship?"},
            headers=auth_headers,
        ) as stream:
            assert stream.status_code == 200
            body = b"".join(stream.iter_bytes()).decode()

    frames = _read_sse(SimpleNamespace(text=body))
    reasoning_frames = [data for event, data in frames if event == "reasoning"]
    delta_frames = [data for event, data in frames if event == "delta"]

    assert reasoning_frames, "expected at least one reasoning frame"
    assert "Let me check the transcript." in "".join(reasoning_frames)
    assert "Tuesday looks right." in "".join(reasoning_frames)
    # The reasoning text must never leak into a delta frame.
    assert not any("Let me check" in d or "looks right" in d for d in delta_frames)

    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    assistant = messages[1]
    assert assistant["content"] == "They agreed to ship on Tuesday."
    assert assistant["reasoning_content"] == "Let me check the transcript. Tuesday looks right."
    # And the answer itself must stay free of reasoning text.
    assert "Let me check" not in assistant["content"]


def test_reasoning_field_is_absent_for_an_ordinary_answer(client, auth_headers, stub_llm):
    """Backward compatibility: a provider that never reasons must not grow the field."""
    conversation = _create(client, auth_headers)

    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "What did the team decide?"},
        headers=auth_headers,
    ) as stream:
        body = b"".join(stream.iter_bytes()).decode()

    events = [event for event, _ in _read_sse(SimpleNamespace(text=body))]
    assert "reasoning" not in events

    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    assert messages[1]["reasoning_content"] is None


def test_first_exchange_titles_the_conversation(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)

    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "What were the action items?"},
        headers=auth_headers,
    ) as stream:
        b"".join(stream.iter_bytes())

    body = client.get(
        f"/api/chat/conversations/{conversation['uuid']}", headers=auth_headers
    ).json()
    assert body["title"] == "What were the action items?"


def test_provider_error_is_delivered_in_band(client, auth_headers):
    """Once the stream opens the status line is committed — errors are frames."""
    failing = _FakeLLM(events=[LLMStreamEvent(type="error", message="provider exploded")])
    with (
        patch("app.services.llm_service.LLMService.create_from_settings", return_value=failing),
        patch("app.services.chat.retrieval.retrieve_chunks", return_value=[]),
    ):
        conversation = _create(client, auth_headers)
        with client.stream(
            "POST",
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "hi"},
            headers=auth_headers,
        ) as stream:
            assert stream.status_code == 200
            body = b"".join(stream.iter_bytes()).decode()

    assert "event: error" in body


def test_missing_llm_is_a_clean_400_not_a_broken_stream(client, auth_headers):
    """Resolved BEFORE StreamingResponse is constructed, so it stays an HTTP code."""
    with patch("app.services.llm_service.LLMService.create_from_settings", return_value=None):
        conversation = _create(client, auth_headers)
        response = client.post(
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "hi"},
            headers=auth_headers,
        )

    assert response.status_code == 400
    assert "llm" in response.json()["detail"].lower()


def test_regenerate_supersedes_the_previous_answer(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)
    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "First question"},
        headers=auth_headers,
    ) as stream:
        b"".join(stream.iter_bytes())

    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/regenerate",
        headers=auth_headers,
    ) as stream:
        assert stream.status_code == 200
        b"".join(stream.iter_bytes())

    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    statuses = [m["status"] for m in messages]

    assert "superseded" in statuses
    assert statuses[-1] == "complete"


def test_regenerate_with_nothing_to_regenerate_is_400(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)
    response = client.post(
        f"/api/chat/conversations/{conversation['uuid']}/regenerate", headers=auth_headers
    )
    assert response.status_code == 400


def test_edit_rewrites_the_question_and_retires_the_tail(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)
    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "Original question"},
        headers=auth_headers,
    ) as stream:
        b"".join(stream.iter_bytes())

    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    user_uuid = messages[0]["uuid"]

    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages/{user_uuid}/edit",
        json={"content": "Corrected question"},
        headers=auth_headers,
    ) as stream:
        assert stream.status_code == 200
        b"".join(stream.iter_bytes())

    after = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    live = [m for m in after if m["status"] != "superseded"]

    assert [m["content"] for m in live if m["role"] == "user"] == ["Corrected question"]
    # The original is retired, not deleted — the audit trail survives.
    assert any(m["status"] == "superseded" for m in after)


def test_a_conversation_deleted_mid_turn_is_404_not_500(
    client, auth_headers, stub_llm, monkeypatch
):
    """The row can vanish between the read and the write, and that is a 404.

    Another tab, another device, or the retention sweep can delete the
    conversation after the handler has loaded it. SQLAlchemy answers the
    resulting zero-row UPDATE with ``StaleDataError``, which reached the client
    as a **500** — "expected to update 1 row(s); 0 were matched". A 500 says the
    server is broken; the truth is that the thing being written to is gone.

    The delete is simulated at the commit rather than issued from a second
    session, because the savepoint harness keeps a concurrent session from
    seeing — let alone deleting — this test's uncommitted row. The seam is the
    exact operation that fails in production, and the assertions are on real
    outcomes: the status code, and that no message was persisted.
    """
    from sqlalchemy.orm import Session as _Session
    from sqlalchemy.orm.exc import StaleDataError

    conversation = _create(client, auth_headers)
    real_commit = _Session.commit
    fired = {"count": 0}

    def _vanish(self, *args, **kwargs):
        # Only the FIRST commit of the turn — the one that writes the user
        # message and touches `last_message_at`. Raising on every commit would
        # also break the rollback path and prove nothing about this branch.
        if fired["count"] == 0:
            fired["count"] += 1
            raise StaleDataError(
                "UPDATE statement on table 'chat_conversation' expected to "
                "update 1 row(s); 0 were matched."
            )
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(_Session, "commit", _vanish)
    response = client.post(
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "Question into the void"},
        headers=auth_headers,
    )
    monkeypatch.undo()

    assert fired["count"] == 1, "the simulated concurrent delete never fired"
    assert response.status_code == 404, (
        f"a vanished conversation must be 404, got {response.status_code}"
    )
    assert "no longer exists" in response.json()["detail"]

    # Real state, not just the status line: the refused turn left nothing behind.
    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    )
    assert messages.status_code == 200
    assert messages.json()["messages"] == [], "a refused turn must persist no message"


def test_an_ordinary_turn_still_commits(client, auth_headers, stub_llm):
    """The control for the case above.

    Without it, "returns 404" also passes for a handler that refuses every turn
    — which would look like a robustness fix and be a total outage.
    """
    conversation = _create(client, auth_headers)
    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "An ordinary question"},
        headers=auth_headers,
    ) as stream:
        assert stream.status_code == 200
        b"".join(stream.iter_bytes())

    messages = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/messages", headers=auth_headers
    ).json()["messages"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["An ordinary question"]


def test_editing_another_users_message_is_404(
    client, auth_headers, other_user_auth_headers, stub_llm
):
    theirs = _create(client, other_user_auth_headers)
    response = client.post(
        f"/api/chat/conversations/{theirs['uuid']}/messages/{uuid_pkg.uuid4()}/edit",
        json={"content": "hijack"},
        headers=auth_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Abuse limits
# ---------------------------------------------------------------------------


def test_hourly_limit_returns_429_with_retry_after(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)
    with patch("app.services.chat.limits.check_hourly_limit", return_value=(False, 1800)):
        response = client.post(
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "hi"},
            headers=auth_headers,
        )

    assert response.status_code == 429
    assert response.headers.get("retry-after") == "1800"


def test_concurrent_stream_cap_returns_429(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers)
    with patch("app.services.chat.limits.acquire_stream_slot", return_value=None):
        response = client.post(
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "hi"},
            headers=auth_headers,
        )

    assert response.status_code == 429


def test_a_refused_send_releases_the_stream_slot(client, auth_headers):
    """Otherwise one failed send would permanently consume a concurrency slot."""
    conversation = _create(client, auth_headers)
    with (
        patch("app.services.llm_service.LLMService.create_from_settings", return_value=None),
        patch("app.services.chat.limits.acquire_stream_slot", return_value="slot-test"),
        patch("app.services.chat.limits.release_stream_slot") as release,
    ):
        response = client.post(
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "hi"},
            headers=auth_headers,
        )

    # The refusal has to have happened where the test thinks it did. Without this the
    # send could 404 (wrong uuid) or 422 (payload drift) and never reach the LLM-config
    # branch at all, while `release` still fires from the outer `finally` — a pass that
    # proves nothing about the path the test is named for.
    assert response.status_code == 400
    assert "No LLM is configured" in response.json()["detail"]
    assert release.call_count == 1
    # The slot that was ACQUIRED. Releasing by user alone would free whichever stream
    # that user happens to have in flight (see `test_release_removes_only_its_own_slot`).
    assert release.call_args.args[1] == "slot-test"


# ---------------------------------------------------------------------------
# Context estimate
# ---------------------------------------------------------------------------


def test_estimate_reports_a_shape_the_picker_can_render(client, auth_headers):
    response = client.post(
        "/api/chat/context/estimate",
        json={"file_uuids": [], "collection_uuids": [], "tag_names": [], "speakers": []},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "file_count",
        "estimated_tokens",
        "context_window",
        "pct",
        "warning_level",
    }
    assert body["warning_level"] in ("ok", "warn", "over")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_returns_markdown_as_an_attachment(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers, title="Export me")
    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "What happened?"},
        headers=auth_headers,
    ) as stream:
        b"".join(stream.iter_bytes())

    response = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/export", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "What happened?" in response.text


def test_export_json_carries_the_messages(client, auth_headers, stub_llm):
    conversation = _create(client, auth_headers, title="JSON export")
    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation['uuid']}/messages",
        json={"content": "Question"},
        headers=auth_headers,
    ) as stream:
        b"".join(stream.iter_bytes())

    body = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/export",
        params={"format": "json"},
        headers=auth_headers,
    ).json()

    assert body["title"] == "JSON export"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_export_rejects_an_unknown_format(client, auth_headers):
    conversation = _create(client, auth_headers)
    response = client.get(
        f"/api/chat/conversations/{conversation['uuid']}/export",
        params={"format": "pdf"},
        headers=auth_headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_disabling_the_capability_hides_the_whole_router(client, auth_headers):
    """404 (not 403): a disabled surface should not exist for this deployment."""
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    def without_chat(_request):
        return {"chat.rag": False}

    set_capability_resolver(without_chat)
    try:
        # platform_admin_bypass would let a superuser through; auth_headers is a
        # normal user, which is the case that matters here.
        response = client.get("/api/chat/conversations", headers=auth_headers)
        assert response.status_code == 404
    finally:
        reset_capability_resolver()

    assert client.get("/api/chat/conversations", headers=auth_headers).status_code == 200
