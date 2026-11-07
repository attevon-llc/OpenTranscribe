"""``GET /api/search/count`` must not be a content oracle for a taken-down file (#817).

Before this fix the handler took a ``file_uuid`` straight to
``HybridSearchService.count_matches`` with no permission check at all — the ONLY
indexed-content read in the whole API with that property. A caller who had learned a
quarantined file's uuid (a stale bookmark, a DevTools history entry, a shared link)
could keep polling ``/count?file_uuid=<uuid>&q=<term>`` and learn, term by term,
whether that word appears anywhere in a transcript they can no longer even open —
worse than a plain 404, because it kept answering.

It also **failed open**: an OpenSearch error degraded to ``{"total": 0}``, which is
indistinguishable from "no matches", so a caller could not tell "nothing found" from
"the count could not be computed" (and a scoped caller would read the *0* as proof the
word is absent from a file it might actually appear in).

A resolved ``file_uuid`` goes through ``get_file_by_uuid_with_permission`` (the
chokepoint that 404s a quarantined file for everyone the review exemption does not
reach) before OpenSearch is ever touched; an unscoped count excludes quarantined files
inside the query through the ``availability`` field the takedown stamps (issue #1253),
and a failed query answers **503**, never ``0``.

OpenSearch is stubbed here: what is under test is the refusal to reach OpenSearch when
scoped to something the caller cannot see, and what a cluster obeying the body would
count when it does.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.file_availability import index_availability
from app.services.search import hybrid_search_service as hss
from tests.fixtures.opensearch_filter_eval import matches_all

COUNT = "/api/search/count"


class _FakeCountEngine:
    """Stub OpenSearch client for ``count_matches`` only. Records every body.

    With ``docs`` it counts the documents the body's ``filter`` list admits (the text
    match is assumed); without, it answers ``total``.
    """

    def __init__(self, total: int = 0, error: Exception | None = None) -> None:
        self.total = total
        self.error = error
        self.docs: list[dict[str, Any]] | None = None
        self.bodies: list[dict[str, Any]] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.bodies.append(body)
        if self.error is not None:
            raise self.error
        if self.docs is None:
            return {"hits": {"total": {"value": self.total}}}
        filters = body["query"]["bool"]["filter"]
        counted = [d for d in self.docs if matches_all(d, filters)]
        return {"hits": {"total": {"value": len(counted)}}}


@pytest.fixture
def fake_count_engine(monkeypatch) -> _FakeCountEngine:
    """Point ``count_matches`` at a stub engine and skip infra bootstrapping."""
    fake = _FakeCountEngine()
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: fake)
    monkeypatch.setattr(hss, "_ensure_infrastructure", lambda *a, **kw: None)
    return fake


def _make_file(db_session, owner: User, **overrides) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    defaults = dict(
        uuid=file_uuid,
        user_id=owner.id,
        filename="count-quarantine.wav",
        storage_path=f"x/{file_uuid.hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
    )
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# 1. A scoped count on a quarantined file must be refused before OpenSearch
# ---------------------------------------------------------------------------


def test_a_scoped_count_is_refused_for_a_quarantined_file(
    client, user_token_headers, normal_user, db_session, fake_count_engine
):
    """The finding: the count must not act as a content oracle for a taken-down file."""
    hidden = _make_file(db_session, normal_user, is_quarantined=True)

    response = client.get(
        COUNT, headers=user_token_headers, params={"q": "pricing", "file_uuid": str(hidden.uuid)}
    )

    assert response.status_code == 404, response.text
    assert fake_count_engine.bodies == [], (
        "OpenSearch was reached even though the file is quarantined — a caller "
        "could still learn whether the term appears in a taken-down transcript"
    )


# ---------------------------------------------------------------------------
# 2. Control: a non-quarantined scoped count still works
# ---------------------------------------------------------------------------


def test_a_scoped_count_still_works_for_a_file_that_is_not_quarantined(
    client, user_token_headers, normal_user, db_session, fake_count_engine
):
    """Without this control, the refusal above would also pass on a broken route."""
    fake_count_engine.total = 3
    visible = _make_file(db_session, normal_user)

    response = client.get(
        COUNT, headers=user_token_headers, params={"q": "pricing", "file_uuid": str(visible.uuid)}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"total": 3}
    assert len(fake_count_engine.bodies) == 1


# ---------------------------------------------------------------------------
# 3. Admin bypass control
# ---------------------------------------------------------------------------


def test_an_admin_can_still_count_within_a_quarantined_file(
    client, admin_token_headers, admin_user, db_session, fake_count_engine
):
    """An admin reviewing a taken-down file must still be able to use the find bar."""
    fake_count_engine.total = 5
    hidden = _make_file(db_session, admin_user, is_quarantined=True)

    response = client.get(
        COUNT, headers=admin_token_headers, params={"q": "pricing", "file_uuid": str(hidden.uuid)}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"total": 5}


# ---------------------------------------------------------------------------
# 4. Unscoped: the whole-corpus count excludes quarantined files, via the field
# ---------------------------------------------------------------------------


def _chunk(owner: User, media_file: MediaFile) -> dict[str, Any]:
    """A chunk document as the indexer writes it, availability from the row."""
    return {
        "file_uuid": str(media_file.uuid),
        "accessible_user_ids": [owner.id],
        "doc_type": "chunk",
        "availability": index_availability(media_file),
    }


def test_an_unscoped_count_excludes_quarantined_files(
    client, user_token_headers, normal_user, db_session, fake_count_engine
):
    """The unscoped case has no single resource to permission-check, so the exclusion is
    in the query itself: the ``availability`` term inside the ACL clause, no id list."""
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    visible = _make_file(db_session, normal_user)
    fake_count_engine.docs = [_chunk(normal_user, hidden), _chunk(normal_user, visible)]

    response = client.get(COUNT, headers=user_token_headers, params={"q": "pricing"})

    assert response.status_code == 200, response.text
    assert response.json() == {"total": 1}
    assert str(hidden.uuid) not in str(fake_count_engine.bodies[0])


def test_an_admin_counts_quarantined_files_across_the_corpus(
    client, admin_token_headers, admin_user, db_session, fake_count_engine
):
    """The review exemption reaches the unscoped count too."""
    hidden = _make_file(db_session, admin_user, is_quarantined=True)
    visible = _make_file(db_session, admin_user)
    fake_count_engine.docs = [_chunk(admin_user, hidden), _chunk(admin_user, visible)]

    response = client.get(COUNT, headers=admin_token_headers, params={"q": "pricing"})

    assert response.status_code == 200, response.text
    assert response.json() == {"total": 2}


# ---------------------------------------------------------------------------
# 5. An OpenSearch query failure is 503, never a silent 0
# ---------------------------------------------------------------------------


def test_an_opensearch_error_is_503_not_a_silent_zero(client, user_token_headers, monkeypatch):
    """Before this fix ``count_matches`` swallowed the exception and returned 0 —
    indistinguishable from "no matches found", which a scoped caller could
    misread as proof a term is absent."""
    failing = _FakeCountEngine(error=RuntimeError("simulated cluster timeout"))
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: failing)
    monkeypatch.setattr(hss, "_ensure_infrastructure", lambda *a, **kw: None)

    response = client.get(COUNT, headers=user_token_headers, params={"q": "pricing"})

    assert response.status_code == 503, response.text
    assert response.json() != {"total": 0}
