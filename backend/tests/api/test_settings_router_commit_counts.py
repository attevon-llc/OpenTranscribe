"""Transaction boundaries of the settings routers: commits per request.

The settings routers keep their own ``commit()`` calls while the queries live in services
that never commit. These pin how many times each request commits, so moving a query can
not silently add, drop or reorder a transaction boundary.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event


@pytest.fixture
def commit_counter(db_session):
    counts: list[int] = []

    def count(_session):
        counts.append(1)

    event.listen(db_session, "after_commit", count)
    yield counts
    event.remove(db_session, "after_commit", count)


@pytest.fixture
def commits_for(client, user_token_headers, commit_counter):
    """``commits_for(method, path, json=None)`` -> ``(response, commits the endpoint made)``.

    The authentication layer commits on every authenticated request, so that baseline is
    measured once with a read-only call and subtracted.
    """

    def send(method, path, json=None):
        commit_counter.clear()
        response = client.request(method, path, json=json, headers=user_token_headers)
        return response, len(commit_counter)

    baseline = send("GET", "/api/user-settings/all")[1]

    def measured(method, path, json=None):
        response, total = send(method, path, json)
        return response, total - baseline

    return measured


def test_user_setting_group_endpoints_commit_once_per_write(commits_for):
    response, n = commits_for("PUT", "/api/user-settings/recording", {"recording_quality": "high"})
    assert response.status_code == 200, response.text
    assert n == 1

    assert commits_for("PUT", "/api/user-settings/download", {"audio_only": True})[1] == 1
    assert commits_for("DELETE", "/api/user-settings/download")[1] == 1
    assert commits_for("DELETE", "/api/user-settings/organization-context")[1] == 1
    assert commits_for("GET", "/api/user-settings/organization-context")[1] == 0


def test_llm_settings_create_commits_for_the_row_and_again_for_the_first_activation(commits_for):
    body = {"name": "cfg-a", "provider": "openai", "model_name": "gpt-x", "api_key": "k"}
    created, n = commits_for("POST", "/api/llm-settings", body)
    assert created.status_code == 200, created.text
    assert n == 2

    assert commits_for("POST", "/api/llm-settings", {**body, "name": "cfg-b"})[1] == 1
    assert commits_for("DELETE", "/api/llm-settings/all")[1] == 1


def test_asr_settings_listing_never_commits_and_delete_all_commits_twice(
    commits_for, normal_user, db_session
):
    from app.models.user_asr_settings import UserASRSettings

    db_session.add(
        UserASRSettings(user_id=normal_user.id, name="a", provider="deepgram", model_name="m")
    )
    db_session.flush()

    listed, n = commits_for("GET", "/api/asr-settings")
    assert listed.status_code == 200 and n == 0

    deleted, n = commits_for("DELETE", "/api/asr-settings/all")
    assert deleted.status_code == 200, deleted.text
    # One for the active-pointer clear, one for the delete itself.
    assert n == 2


def test_custom_vocabulary_writes_commit_once(commits_for):
    created, n = commits_for("POST", "/api/custom-vocabulary", {"term": "alpha", "domain": "legal"})
    assert created.status_code == 201, created.text
    assert n == 1

    assert commits_for("DELETE", "/api/custom-vocabulary/all?domain=legal")[1] == 1
    listed, n = commits_for("GET", "/api/custom-vocabulary")
    assert listed.status_code == 200 and n == 0


def test_redaction_preferences_reset_commits_once(commits_for):
    assert commits_for("DELETE", "/api/user-settings/redaction")[1] == 1
