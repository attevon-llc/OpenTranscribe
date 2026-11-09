"""Moving the task and own-file routes' queries into services must not move a commit.

The expected counts were measured with the queries still in the routers. They include the
commits the auth dependency makes on every authenticated request.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.fixtures.tag_world import make_file


@pytest.fixture
def commits():
    seen: list[int] = []

    def _count(_session):
        seen.append(1)

    event.listen(Session, "after_commit", _count)
    yield seen
    event.remove(Session, "after_commit", _count)


def _commits_during(commits, call):
    commits.clear()
    response = call()
    assert response.status_code == 200, response.text
    return len(commits)


def test_own_file_and_task_reads_commit_count(
    client, db_session, normal_user, user_token_headers, admin_token_headers, commits
):
    media = make_file(db_session, normal_user)

    def get(path, headers):
        return lambda: client.get(path, headers=headers)

    assert _commits_during(commits, get("/api/my-files/status", user_token_headers)) == 2
    assert (
        _commits_during(commits, get(f"/api/my-files/{media.uuid}/status", user_token_headers)) == 2
    )
    assert _commits_during(commits, get("/api/tasks", user_token_headers)) == 2
    assert _commits_during(commits, get("/api/tasks/system/health", admin_token_headers)) == 2
