"""Moving a router's queries into a service must not move or add a commit.

Each route is driven once with a counter on ``Session.commit``. The expected counts were
measured on the routes while their queries still lived in the router; they include the
commits the auth dependency makes on every authenticated request, so a change to ANY commit
on the path moves them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.legal import registry

KEY = registry.OPERATOR_ACKNOWLEDGMENT_KEY


@pytest.fixture
def commits():
    seen: list[int] = []

    def _count(_session):
        seen.append(1)

    event.listen(Session, "after_commit", _count)
    yield seen
    event.remove(Session, "after_commit", _count)


def _count_for(commits, call):
    commits.clear()
    response = call()
    assert response.status_code == 200, response.text
    return len(commits)


def test_usage_reads_commit_count(client, user_token_headers, commits):
    assert _count_for(commits, lambda: client.get("/api/usage/me", headers=user_token_headers)) == 2
    assert (
        _count_for(commits, lambda: client.get("/api/usage/me/daily", headers=user_token_headers))
        == 2
    )


def test_legal_listing_commit_count(client, super_admin_token_headers, commits):
    assert (
        _count_for(
            commits,
            lambda: client.get("/api/legal/acknowledgments", headers=super_admin_token_headers),
        )
        == 2
    )


def test_legal_acknowledge_commit_count_first_and_repeat(
    client, super_admin_token_headers, commits
):
    doc = registry.get_document(KEY)
    assert doc is not None
    body = {"key": doc.key, "version": doc.version, "sha256": doc.sha256}

    def post():
        return client.post(
            "/api/legal/acknowledgments", headers=super_admin_token_headers, json=body
        )

    assert _count_for(commits, post) == 3
    assert _count_for(commits, post) == 2


def test_support_access_organization_search_commit_count(support_world, commits):
    w = support_world
    assert (
        _count_for(
            commits,
            lambda: w.client.get(
                "/api/support-access/targets/organizations", headers=w.admin_headers
            ),
        )
        == 2
    )
