"""``GET /search/filters`` must fail CLOSED when quarantine cannot be resolved (issue #876).

``HybridSearchService._quarantined_file_uuids`` is the whole quarantine exclusion
for the facet plane: quarantine is Postgres-only, the chunks index carries no
field to filter on, and the endpoint returns aggregated buckets rather than a hit
list, so there is nothing to post-filter. That makes this function a *takedown
enforcement mechanism*, not an approximate sidebar count — and it used to stop
enforcing in two silent ways:

  * a DB error was swallowed and ``[]`` returned, which means **exclude nothing**;
  * more than ``_QUARANTINED_UUID_CAP`` quarantined files truncated the set with
    no error and no signal.

Both now raise ``QuarantineExclusionUnavailableError`` and the endpoint answers
**503**, matching how ``_summary_search_payload`` withholds results on a masking
outage rather than serving unmasked text.

The DB half runs against the real query (real ``MediaFile`` rows, the real
``limit``), with ``session_scope`` bridged to the savepoint session the way
``tests/integration/test_search_filters_quarantine.py`` does — the function opens
its own session and cannot otherwise see uncommitted rows. The cap is measured
RELATIVE to whatever the connected database already holds, so the test is
deterministic regardless of the dev stack's contents.

OpenSearch is stubbed here on purpose: what is under test is the refusal to reach
OpenSearch at all, plus the shape of the clause when the exclusion does resolve.
That the ``must_not`` genuinely removes documents from ``aggs`` is proven against
a real cluster in ``tests/integration/test_search_filters_quarantine.py``.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.search import hybrid_search_service as hss
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import QuarantineExclusionUnavailableError

DISTINCTIVE_SPEAKER = "Zzyzx-Facet-Speaker"


class _FakeIndices:
    def exists(self, index: str) -> bool:  # noqa: ARG002 - stub signature parity
        return True


class _FakeOpenSearch:
    """Stub cluster that always has one speaker/tag bucket to serve.

    Records every ``search`` body so a test can assert both *that* the cluster was
    reached and *what* clause it was asked for. If a fail-closed path ever reaches
    it, ``bodies`` is non-empty and the test says so.
    """

    def __init__(self) -> None:
        self.indices = _FakeIndices()
        self.bodies: list[dict[str, Any]] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.bodies.append(body)
        return {
            "aggregations": {
                "speakers": {"buckets": [{"key": DISTINCTIVE_SPEAKER, "doc_count": 3}]},
                "tags": {"buckets": [{"key": "zzyzx-tag", "doc_count": 3}]},
                "date_range": {
                    "min_as_string": "2026-01-01T00:00:00Z",
                    "max_as_string": "2026-02-01T00:00:00Z",
                },
            }
        }


@pytest.fixture
def fake_cluster(monkeypatch) -> _FakeOpenSearch:
    """Point the service at a stub cluster that would happily serve facets."""
    fake = _FakeOpenSearch()
    monkeypatch.setattr(hss, "opensearch_client", fake)
    monkeypatch.setattr(hss, "_index_verified", True)
    return fake


@pytest.fixture
def bridged_session(monkeypatch, db_session):
    """Make ``_quarantined_file_uuids``'s own session the test's savepoint session."""

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)
    return db_session


@pytest.fixture
def broken_session(monkeypatch):
    """Make ``_quarantined_file_uuids``'s own session raise, as a DB outage would."""

    @contextlib.contextmanager
    def _boom():
        raise RuntimeError("simulated transient database outage")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr("app.db.session_utils.session_scope", _boom)


def _make_quarantined_file(db_session, owner: User) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename="facet-cap.wav",
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        is_quarantined=True,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _quarantined_count(db_session) -> int:
    return int(db_session.query(MediaFile.uuid).filter(MediaFile.is_quarantined.is_(True)).count())


# ---------------------------------------------------------------------------
# Failure mode 1: the DB cannot be read
# ---------------------------------------------------------------------------


def test_a_db_outage_withholds_facets_instead_of_serving_unfiltered_ones(
    fake_cluster, broken_session
):
    """A DB error must refuse the request, not degrade to "exclude nothing".

    Measured against the pre-fix code: ``_quarantined_file_uuids()`` returned
    ``[]``, the aggregation ran with no ``must_not`` at all, and this call handed
    back a populated facet payload with HTTP 200 — every quarantined file's
    speakers and tags included.
    """
    service = HybridSearchService()

    with pytest.raises(QuarantineExclusionUnavailableError):
        service.get_available_filters(user_id=1, is_admin=False)

    assert fake_cluster.bodies == [], (
        "the aggregation was executed even though the quarantine exclusion could "
        "not be resolved — facets built from taken-down files could be served"
    )


def test_the_endpoint_answers_503_rather_than_facets(
    client, user_token_headers, fake_cluster, broken_session
):
    """The wire contract for the refusal: 503, no facet payload."""
    response = client.get("/api/search/filters", headers=user_token_headers)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Search filters are temporarily unavailable."


def test_an_admin_keeps_facets_through_a_db_outage(fake_cluster, broken_session):
    """Admins never consult the exclusion, so an outage must not 503 their view.

    The control for the fix's regression risk: making the failure loud must not
    take the review surface away from the only role that can release a file.
    """
    service = HybridSearchService()

    result = service.get_available_filters(user_id=1, is_admin=True)

    assert [s["name"] for s in result["speakers"]] == [DISTINCTIVE_SPEAKER]
    assert len(fake_cluster.bodies) == 1


# ---------------------------------------------------------------------------
# Failure mode 2: more quarantined files than the terms clause can carry
# ---------------------------------------------------------------------------


def test_exceeding_the_cap_is_refused_not_silently_truncated(
    monkeypatch, fake_cluster, bridged_session, normal_user
):
    """One file past the cap must raise — a partial exclusion is not an exclusion.

    The cap is set relative to the rows already present so the assertion does not
    depend on what the connected database happens to hold.
    """
    db = bridged_session
    baseline = _quarantined_count(db)
    _make_quarantined_file(db, normal_user)
    _make_quarantined_file(db, normal_user)
    monkeypatch.setattr(hss, "_QUARANTINED_UUID_CAP", baseline + 1)

    service = HybridSearchService()
    with pytest.raises(QuarantineExclusionUnavailableError):
        service.get_available_filters(user_id=normal_user.id, is_admin=False)

    assert fake_cluster.bodies == [], (
        "an aggregation ran against a truncated exclusion set — the extra "
        "quarantined file's facets would have been served"
    )


def test_exactly_at_the_cap_the_exclusion_is_applied_in_full(
    monkeypatch, fake_cluster, bridged_session, normal_user
):
    """The boundary control: at the cap nothing is dropped and nothing is refused.

    Without this, ``test_exceeding_the_cap_...`` would also pass if the function
    raised unconditionally. It also pins the ``limit(cap + 1)`` probe — a plain
    ``limit(cap)`` cannot tell "exactly cap" from "cap and more".
    """
    db = bridged_session
    baseline = _quarantined_count(db)
    first = _make_quarantined_file(db, normal_user)
    second = _make_quarantined_file(db, normal_user)
    monkeypatch.setattr(hss, "_QUARANTINED_UUID_CAP", baseline + 2)

    service = HybridSearchService()
    result = service.get_available_filters(user_id=normal_user.id, is_admin=False)

    assert [s["name"] for s in result["speakers"]] == [DISTINCTIVE_SPEAKER]
    assert len(fake_cluster.bodies) == 1
    excluded = fake_cluster.bodies[0]["query"]["bool"]["must_not"][0]["terms"]["file_uuid"]
    assert str(first.uuid) in excluded
    assert str(second.uuid) in excluded
    assert len(excluded) == baseline + 2


def test_the_resolved_exclusion_becomes_a_must_not_clause(
    fake_cluster, bridged_session, normal_user
):
    """Happy path, unchanged: a resolvable exclusion is applied, not raised over."""
    db = bridged_session
    media_file = _make_quarantined_file(db, normal_user)

    service = HybridSearchService()
    result = service.get_available_filters(user_id=normal_user.id, is_admin=False)

    assert [s["name"] for s in result["speakers"]] == [DISTINCTIVE_SPEAKER]
    assert len(fake_cluster.bodies) == 1
    excluded = fake_cluster.bodies[0]["query"]["bool"]["must_not"][0]["terms"]["file_uuid"]
    assert str(media_file.uuid) in excluded
