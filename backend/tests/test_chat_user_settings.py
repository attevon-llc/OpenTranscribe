"""Per-user and admin chat settings APIs (issue #52).

Both surfaces store into existing tables (``UserSetting`` / ``SystemSettings``)
with coded defaults, so the things worth testing are: unset fields fall back to
the constant rather than to null, a partial update leaves the rest alone, the
validation caps hold, and the admin surface is actually admin-only.
"""

from __future__ import annotations

import pytest

from app.core import constants as C  # noqa: N812

# ---------------------------------------------------------------------------
# Per-user preferences
# ---------------------------------------------------------------------------


def test_defaults_are_returned_before_anything_is_saved(client, auth_headers):
    body = client.get("/api/user-settings/chat", headers=auth_headers).json()

    assert body["system_prompt"] == C.DEFAULT_CHAT_SYSTEM_PROMPT
    assert body["use_context_default"] is C.DEFAULT_CHAT_USE_CONTEXT
    assert body["default_search_mode"] == C.DEFAULT_CHAT_SEARCH_MODE


def test_settings_round_trip(client, auth_headers):
    saved = client.put(
        "/api/user-settings/chat",
        json={
            "system_prompt": "Answer as a concise meeting summary.",
            "use_context_default": False,
            "default_search_mode": "keyword",
        },
        headers=auth_headers,
    ).json()

    assert saved["system_prompt"] == "Answer as a concise meeting summary."
    assert saved["use_context_default"] is False
    assert saved["default_search_mode"] == "keyword"

    # Survives a fresh read, i.e. it really persisted.
    assert client.get("/api/user-settings/chat", headers=auth_headers).json() == saved


def test_partial_update_leaves_other_fields_intact(client, auth_headers):
    client.put(
        "/api/user-settings/chat",
        json={"system_prompt": "Keep it short.", "default_search_mode": "semantic"},
        headers=auth_headers,
    )
    body = client.put(
        "/api/user-settings/chat", json={"use_context_default": False}, headers=auth_headers
    ).json()

    assert body["system_prompt"] == "Keep it short."
    assert body["default_search_mode"] == "semantic"
    assert body["use_context_default"] is False


def test_false_is_stored_rather_than_treated_as_unset(client, auth_headers):
    """A classic falsy-value bug: `if value:` would silently drop this."""
    client.put("/api/user-settings/chat", json={"use_context_default": False}, headers=auth_headers)
    assert (
        client.get("/api/user-settings/chat", headers=auth_headers).json()["use_context_default"]
        is False
    )


def test_system_prompt_is_length_capped(client, auth_headers):
    response = client.put(
        "/api/user-settings/chat", json={"system_prompt": "x" * 2001}, headers=auth_headers
    )
    assert response.status_code == 422


def test_invalid_search_mode_is_rejected(client, auth_headers):
    response = client.put(
        "/api/user-settings/chat", json={"default_search_mode": "vibes"}, headers=auth_headers
    )
    assert response.status_code == 422


def test_reset_restores_the_coded_defaults(client, auth_headers):
    client.put(
        "/api/user-settings/chat",
        json={"system_prompt": "Custom", "use_context_default": False},
        headers=auth_headers,
    )
    client.delete("/api/user-settings/chat", headers=auth_headers)

    body = client.get("/api/user-settings/chat", headers=auth_headers).json()
    assert body["system_prompt"] == C.DEFAULT_CHAT_SYSTEM_PROMPT
    assert body["use_context_default"] is C.DEFAULT_CHAT_USE_CONTEXT


def test_settings_are_per_user(client, auth_headers, other_user_auth_headers):
    client.put("/api/user-settings/chat", json={"system_prompt": "Mine only"}, headers=auth_headers)

    theirs = client.get("/api/user-settings/chat", headers=other_user_auth_headers).json()
    assert theirs["system_prompt"] != "Mine only"


# ---------------------------------------------------------------------------
# Admin RAG settings
# ---------------------------------------------------------------------------


def test_admin_settings_expose_the_coded_defaults(client, admin_auth_headers):
    body = client.get("/api/admin/chat-settings", headers=admin_auth_headers).json()

    assert body["candidate_pool"] == C.DEFAULT_CHAT_RAG_CANDIDATE_POOL
    assert body["final_chunks"] == C.DEFAULT_CHAT_RAG_FINAL_CHUNKS
    assert body["rerank_enabled"] is C.DEFAULT_CHAT_RAG_RERANK_ENABLED
    assert body["retention_days"] == C.DEFAULT_CHAT_RETENTION_DAYS


def test_admin_settings_round_trip(client, admin_auth_headers):
    saved = client.put(
        "/api/admin/chat-settings",
        json={"candidate_pool": 64, "rerank_enabled": False, "retention_days": 30},
        headers=admin_auth_headers,
    ).json()

    assert saved["candidate_pool"] == 64
    assert saved["rerank_enabled"] is False
    assert saved["retention_days"] == 30
    assert client.get("/api/admin/chat-settings", headers=admin_auth_headers).json() == saved


def test_admin_settings_are_range_checked(client, admin_auth_headers):
    for payload in (
        {"candidate_pool": 0},
        {"candidate_pool": 10_000},
        {"semantic_cache_threshold": 0.1},
        {"retention_days": -1},
    ):
        response = client.put("/api/admin/chat-settings", json=payload, headers=admin_auth_headers)
        assert response.status_code == 422, payload


def test_empty_admin_update_is_rejected(client, admin_auth_headers):
    response = client.put("/api/admin/chat-settings", json={}, headers=admin_auth_headers)
    assert response.status_code == 400


@pytest.mark.parametrize("method", ["get", "put"])
def test_admin_settings_are_not_reachable_by_a_normal_user(client, auth_headers, method):
    kwargs = {"headers": auth_headers}
    if method == "put":
        kwargs["json"] = {"candidate_pool": 1}
    response = getattr(client, method)("/api/admin/chat-settings", **kwargs)
    assert response.status_code == 403, response.text


def test_admin_changes_are_read_by_the_service_layer(client, admin_auth_headers, db_session):
    """The knobs must actually drive retrieval, not just persist."""
    from app.services.chat.settings import get_chat_settings

    client.put(
        "/api/admin/chat-settings",
        json={"final_chunks": 7, "rerank_enabled": False},
        headers=admin_auth_headers,
    )

    resolved = get_chat_settings(db_session)
    assert resolved.final_chunks == 7
    assert resolved.rerank_enabled is False


def test_retrieval_settings_revision_changes_with_a_retune(client, admin_auth_headers, db_session):
    """The revision keys the retrieval cache — a retune must invalidate it."""
    from app.services.chat.settings import get_chat_settings

    before = get_chat_settings(db_session).revision
    client.put("/api/admin/chat-settings", json={"final_chunks": 9}, headers=admin_auth_headers)
    assert get_chat_settings(db_session).revision != before
