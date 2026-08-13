"""What an UNSCOPED conversation actually searches (issue #438).

The report behind #438 was that a conversation created with no scope retrieves
nothing, and blamed the create-time default: ``ConversationCreate.scope``
defaults to an all-empty :class:`ChatScope`, and the repo's own warning says
``file_uuids == []`` means "match nothing".

That inference is wrong, and these tests are the falsification. The default is a
scope-level empty, not a resolved-to-empty file list: ``scope.is_empty`` sends
``resolve_scope_file_uuids`` down the ``return None`` branch, and ``None`` means
"every transcript the caller can access". The observed zero-retrieval run was an
OpenSearch ``503 search_phase_execution_exception`` during a reindex, swallowed
by ``retrieve_chunks``' fail-soft handler — which is why the second half of #438
(surface a zero-excerpt answer) is the fix that matters.

So the tests here pin the chain end to end through the real HTTP endpoint rather
than at the resolver (``test_chat_context_resolver.py`` already covers that
unit), and pin the SAFETY direction beside it: widening nothing must still leave
an unauthorized *explicit* selection matching nothing.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.models.media import MediaFile
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import retrieve_chunks


class _FakeLLM:
    def __init__(self):
        self.config = SimpleNamespace(
            provider=SimpleNamespace(value="openai"), model="test-model", temperature=0.3
        )
        self.user_context_window = 8192
        self.response_tokens = 1000

    def chat_completion_stream(self, messages, cancel_event=None, **kwargs):
        yield LLMStreamEvent(type="delta", text="An answer.")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


@pytest.fixture
def captured_retrieval(db_session):
    """Run a real send, capturing the kwargs retrieval was called with.

    Only the provider and the OpenSearch call are stubbed: the endpoint, scope
    resolution, and the JSONB round trip through ``chat_conversation.context``
    all run for real, because the defaulting chain under test lives in exactly
    those seams.
    """

    @contextmanager
    def _test_session_scope():
        yield db_session
        db_session.commit()

    spy = MagicMock(return_value=[])
    with (
        patch("app.services.llm_service.LLMService.create_from_settings", return_value=_FakeLLM()),
        patch("app.services.llm_service.LLMService.create_from_config_id", return_value=_FakeLLM()),
        patch("app.services.chat.retrieval.retrieve_chunks", spy),
        patch("app.db.session_utils.session_scope", _test_session_scope),
    ):
        yield spy


def _send(client, headers, conversation_uuid: str, content: str = "What did we decide?") -> None:
    with client.stream(
        "POST",
        f"/api/chat/conversations/{conversation_uuid}/messages",
        json={"content": content},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200, stream.read()
        b"".join(stream.iter_bytes())


def _create(client, headers, **body) -> dict:
    response = client.post("/api/chat/conversations", json=body or {}, headers=headers)
    assert response.status_code == 201, response.text
    created: dict = response.json()
    return created


def _persisted_metadata(client, headers, conversation_uuid: str) -> dict:
    """The assistant row's ``msg_metadata`` — the same record the UI reads back.

    ``files_searched`` is the durable evidence of which branch ran: the string
    ``"all"`` for the unscoped default, an integer for a resolved selection. It
    is asserted alongside the retrieval spy so these tests describe state the
    product actually keeps, not only a call that was made.
    """
    response = client.get(f"/api/chat/conversations/{conversation_uuid}/messages", headers=headers)
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert assistant, "the turn did not persist an assistant message"
    return assistant[-1]["msg_metadata"] or {}


def _file_for(db, user, *, title="Theirs") -> MediaFile:
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


# ---------------------------------------------------------------------------
# The default: unscoped means EVERYTHING accessible
# ---------------------------------------------------------------------------


def test_a_conversation_created_without_a_scope_searches_all_accessible_files(
    client, auth_headers, captured_retrieval
):
    """#438's claim, tested: the create-time default must not mean "nothing".

    ``None`` — not ``[]`` — is what reaches retrieval. An empty list would make
    ``retrieve_chunks`` short-circuit before it ever queried OpenSearch.
    """
    conversation = _create(client, auth_headers, title="Unscoped")
    assert conversation["scope"] == {
        "file_uuids": [],
        "collection_uuids": [],
        "tag_names": [],
        "speakers": [],
    }

    _send(client, auth_headers, conversation["uuid"])

    assert captured_retrieval.call_count == 1
    assert captured_retrieval.call_args.kwargs["file_uuids"] is None
    assert _persisted_metadata(client, auth_headers, conversation["uuid"])["files_searched"] == (
        "all"
    )


def test_omitting_the_scope_and_sending_an_empty_one_behave_identically(
    client, auth_headers, captured_retrieval
):
    """The API has no way to express "match nothing", and that is deliberate.

    A caller who sends all-empty lists gets the same "all accessible" resolution
    as one who sends no scope at all — ``ChatScope.is_empty`` is what both
    produce. "Match nothing" is reachable only as the *result* of resolving a
    selection the caller may not read, which the tests below pin.
    """
    explicit = _create(
        client,
        auth_headers,
        title="Explicitly empty",
        scope={"file_uuids": [], "collection_uuids": [], "tag_names": [], "speakers": []},
    )

    _send(client, auth_headers, explicit["uuid"])

    assert captured_retrieval.call_args.kwargs["file_uuids"] is None
    assert _persisted_metadata(client, auth_headers, explicit["uuid"])["files_searched"] == "all"


# ---------------------------------------------------------------------------
# The safety direction: the default widens NOTHING it should not
# ---------------------------------------------------------------------------


def test_a_scope_naming_another_users_file_still_matches_nothing(
    client, auth_headers, db_session, other_user, captured_retrieval
):
    """The leak the ``None``/``[]`` warning guards against.

    An unauthorized selection resolves to an empty LIST, not to ``None``. If the
    two were conflated, asking about someone else's recording would silently
    search the caller's whole library instead of answering "nothing matched".
    """
    theirs = _file_for(db_session, other_user)
    conversation = _create(
        client, auth_headers, title="Someone else's file", scope={"file_uuids": [str(theirs.uuid)]}
    )

    _send(client, auth_headers, conversation["uuid"])

    assert captured_retrieval.call_args.kwargs["file_uuids"] == []
    assert _persisted_metadata(client, auth_headers, conversation["uuid"])["files_searched"] == 0


def test_an_unresolvable_scope_does_not_fall_back_to_the_unscoped_default(
    client, auth_headers, captured_retrieval
):
    """A uuid that matches no row is "nothing", not "everything"."""
    conversation = _create(client, auth_headers, scope={"file_uuids": [str(uuid_pkg.uuid4())]})

    _send(client, auth_headers, conversation["uuid"])

    assert captured_retrieval.call_args.kwargs["file_uuids"] == []
    assert _persisted_metadata(client, auth_headers, conversation["uuid"])["files_searched"] == 0


def test_an_empty_resolved_scope_never_reaches_opensearch():
    """The short-circuit that makes ``[]`` mean "nothing" rather than "no filter".

    Without it an empty ``terms`` clause would be dropped by the query builder
    and the search would run unfiltered — the leak, one layer lower.
    """
    with patch("app.services.search.chunk_retrieval.get_opensearch_client") as get_client:
        assert retrieve_chunks("what did we decide", user_id=7, file_uuids=[]) == []
        get_client.assert_not_called()


def test_the_unscoped_query_is_still_gated_by_the_callers_access_list():
    """ "All accessible" is enforced in the query, not by enumerating files.

    This is the term that makes the default safe: with no ``file_uuid`` filter at
    all, ``accessible_user_ids`` is the ONLY thing standing between one user's
    question and another user's transcripts.
    """
    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    with patch("app.services.search.chunk_retrieval.get_opensearch_client", return_value=client):
        retrieve_chunks("what did we decide", user_id=42, file_uuids=None, search_mode="keyword")

    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"accessible_user_ids": [42]}} in filters
    assert not any("file_uuid" in clause.get("terms", {}) for clause in filters)
