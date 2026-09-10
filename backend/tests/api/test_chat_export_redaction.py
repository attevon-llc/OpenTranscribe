"""A chat export must resolve a redaction policy, and its CITATIONS are the hole (issue #863).

``backend/app/api/endpoints/chat/export.py`` resolved **no** redaction policy of any kind, so
``GET /api/chat/conversations/{uuid}/export`` was the one transcript-bearing export surface
that #673/#85 never reached.

Two of the three fields it emits were already safe, and re-masking them was explicitly out of
scope for the fix:

* ``content`` and ``reasoning_content`` are masked **at persist time** by
  ``chat/output_redactor.OutputRedactor`` — ``chat/CLAUDE.md``'s "The persisted answer is the
  masked one". ``test_persisted_content_is_the_masked_one`` below pins that, so a future change
  that moves output redaction out of the persist path fails HERE rather than silently making
  this module's scope wrong.
* ``citations[].snippet`` was **not**. A citation snippet is a slice of
  ``chat/redactor.MaskedChunk.content``, whose masking answers the *egress* question ("may this
  text be sent to a provider"), and on a deployment running a **local** model that question is
  answered "no masking needed" by design (``redaction/llm_guard.is_local_provider``). So the
  snippet persisted in ``chat_message.citations`` can be raw transcript text, and the export
  wrote it to a file the user keeps.

Every test here therefore persists a citation whose snippet holds unmasked content — which is
exactly the shape a local-LLM deployment produces — and asserts the *export* masks it under the
requesting user's effective policy, with the admin ``export_locked`` floor folded in.

The profanity detector is used throughout because it is a wordlist, so these run with no
Presidio/spaCy weights installed and in CPU-only CI.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from fastapi import status

from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage

pytestmark = pytest.mark.xdist_group("chat_export_redaction_settings")

PROFANITY = "damn"
SNIPPET = f"Priya said this quarter was a {PROFANITY} disaster for the retail team"
CLEAN_ANSWER = "The retail team missed its target."

_USER_PREFS = "/api/user-settings/redaction"
_ADMIN_POLICY = "/api/admin/redaction-policy"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _set_admin_policy(client, super_admin_token_headers, **body) -> None:
    resp = client.post(f"{_ADMIN_POLICY}/update", json=body, headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text


def _set_user_redaction(client, user_token_headers, **body) -> None:
    resp = client.put(_USER_PREFS, json=body, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text


def _conversation_with_citation(
    db_session, owner, *, snippet: str = SNIPPET, answer: str = CLEAN_ANSWER
) -> ChatConversation:
    """One question/answer pair whose answer cites a chunk with ``snippet``."""
    conversation = ChatConversation(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        title="export863",
        context={},
    )
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)

    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_USER,
            content="How did retail do?",
        )
    )
    db_session.add(
        ChatMessage(
            uuid=uuid_pkg.uuid4(),
            conversation_id=conversation.id,
            role=ROLE_ASSISTANT,
            content=answer,
            citations=[
                {
                    "id": 1,
                    "kind": "chunk",
                    "file_uuid": str(uuid_pkg.uuid4()),
                    "title": "Retail review",
                    "chunk_index": 3,
                    "start_time": 61.0,
                    "end_time": 92.0,
                    "speaker": "Priya",
                    "snippet": snippet,
                    "expanded": False,
                }
            ],
        )
    )
    db_session.commit()
    return conversation


def _export(client, headers, conversation, fmt: str = "markdown"):
    return client.get(
        f"/api/chat/conversations/{conversation.uuid}/export",
        headers=headers,
        params={"format": fmt},
    )


# --------------------------------------------------------------------------- #
# The hole: citations[].snippet
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_a_citation_snippet_is_masked_in_the_export(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """The user's own policy masks the snippet the export writes to disk."""
    _set_admin_policy(client, super_admin_token_headers, force_export_redacted=False)
    _set_user_redaction(client, user_token_headers, enabled=True, categories=["profanity"])
    conversation = _conversation_with_citation(db_session, normal_user)

    response = _export(client, user_token_headers, conversation, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text, (
        f"the {fmt} chat export wrote an unmasked citation snippet to a downloaded file"
    )
    # The rest of the citation is untouched — masking must not blank the source list.
    assert "Retail review" in response.text


def test_the_json_export_masks_the_snippet_inside_the_citation_object(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session
):
    """Pinned on the parsed payload, not on a substring of the whole body.

    A body-level ``PROFANITY not in response.text`` also passes if the export dropped the
    citations array entirely; this asserts the citation survives AND its snippet is masked.
    """
    _set_admin_policy(client, super_admin_token_headers, force_export_redacted=False)
    _set_user_redaction(client, user_token_headers, enabled=True, categories=["profanity"])
    conversation = _conversation_with_citation(db_session, normal_user)

    response = _export(client, user_token_headers, conversation, "json")

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = json.loads(response.text)
    citations = [c for m in payload["messages"] for c in m["citations"]]
    assert len(citations) == 1, "the export dropped the citation instead of masking it"
    snippet = citations[0]["snippet"]
    assert PROFANITY not in snippet
    assert "Priya said this quarter was a" in snippet, "masking rewrote more than the span"


def test_the_admin_export_floor_reaches_a_chat_export(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session
):
    """The admin floor applies to a user who has redaction switched OFF for themselves.

    This is the test that goes red if the policy resolution is removed from the export
    handler: with no user preference to inherit, ONLY the resolved admin floor can mask.
    """
    _set_user_redaction(client, user_token_headers, enabled=False)
    _set_admin_policy(
        client, super_admin_token_headers, force_profanity=True, force_export_redacted=True
    )
    conversation = _conversation_with_citation(db_session, normal_user)

    response = _export(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text, (
        "the admin export floor was bypassable through the chat export"
    )


def test_the_local_provider_exemption_is_not_inherited_by_the_export(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session
):
    """``redact_before_llm`` OFF is an EGRESS decision and must not unmask an export.

    On a local-LLM deployment the persisted snippet is raw by design. The export is a
    different decision — a file the user keeps — and keys off the export plane, not the
    egress plane. A fix wired to ``cfg.redact_before_llm`` would go green everywhere else
    in this module and red here.
    """
    _set_admin_policy(client, super_admin_token_headers, force_export_redacted=False)
    _set_user_redaction(
        client,
        user_token_headers,
        enabled=True,
        categories=["profanity"],
        redact_before_llm=False,
    )
    conversation = _conversation_with_citation(db_session, normal_user)

    response = _export(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text


# --------------------------------------------------------------------------- #
# Controls — a policy that masks nothing must leave the export byte-identical
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_no_policy_leaves_the_snippet_untouched(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """The must-not-fire control: "mask unconditionally" also passes every test above."""
    _set_admin_policy(client, super_admin_token_headers, force_export_redacted=False)
    _set_user_redaction(client, user_token_headers, enabled=False)
    conversation = _conversation_with_citation(db_session, normal_user)

    response = _export(client, user_token_headers, conversation, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY in response.text, (
        f"the {fmt} export masked a snippet with no policy asking it to"
    )


def test_a_category_the_user_does_not_mask_is_left_alone(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session
):
    """Redaction ON but only for ``custom`` words: profanity stays in the clear.

    Without this, an implementation that ignores ``enabled_categories`` and always runs
    every detector would pass the whole module.
    """
    _set_admin_policy(client, super_admin_token_headers, force_export_redacted=False)
    _set_user_redaction(
        client, user_token_headers, enabled=True, categories=["custom"], custom_words=["zylofenix"]
    )
    conversation = _conversation_with_citation(db_session, normal_user)

    response = _export(client, user_token_headers, conversation)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY in response.text


# --------------------------------------------------------------------------- #
# The scope boundary this module relies on
# --------------------------------------------------------------------------- #


def test_persisted_content_is_the_masked_one():
    """``content``/``reasoning_content`` are masked BEFORE they are stored, so the export
    inherits that and #863's fix deliberately does not re-mask them.

    If output redaction ever stops running on the persist path, this module's premise —
    "citations are the only unmasked field" — becomes false, and this test says so.
    Structural rather than behavioural because the behaviour it guards is already covered
    by ``tests/unit/test_chat_output_redaction.py``; what is NOT covered anywhere else is
    that #863's scope depends on it.
    """
    import inspect

    from app.services.chat import service as chat_service

    source = inspect.getsource(chat_service)
    assert "_redact_delta(answer_redactor" in source, (
        "the answer stream no longer passes through OutputRedactor before being accumulated; "
        "chat export now needs to mask `content` too (issue #863)"
    )
    assert "_redact_delta(reasoning_redactor" in source, (
        "the reasoning stream no longer passes through OutputRedactor before being "
        "accumulated; chat export now needs to mask `reasoning_content` too (issue #863)"
    )
    assert "content=turn.answer" in source, (
        "the persisted message no longer stores the redacted answer accumulation"
    )
    assert "reasoning_content=turn.reasoning" in source, (
        "the persisted message no longer stores the redacted reasoning accumulation"
    )
