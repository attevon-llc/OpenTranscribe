"""End-to-end check of the neural-search bootstrap self-heal against a real cluster (#625).

Everything in ``backend/tests/unit/test_neural_bootstrap.py`` exercises the sequencing logic
against stand-ins. This file is the one thing a stand-in cannot prove: that
``ensure_neural_search_bootstrap()`` really reaches a healthy state against real OpenSearch
ML Commons, and that a SECOND call is a true no-op — no ``put_pipeline`` call — rather than
merely returning the same *result*.

Point at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 pytest backend/tests/integration/test_neural_bootstrap_selfheal.py \\
        -m integration
"""

from __future__ import annotations

import os

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). This suite proves the bootstrap "
            "against a REAL ML Commons deployment; a stand-in cannot supply that."
        ),
    ),
]


def test_bootstrap_reaches_ok_and_is_idempotent():
    """First call establishes (or confirms) a healthy state; the second is a pure no-op."""
    from app.core.config import settings
    from app.services.search.neural_bootstrap import ensure_neural_search_bootstrap

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        pytest.skip("Neural search disabled on this deployment")

    first = ensure_neural_search_bootstrap()
    assert first.state == "ok", (
        f"First bootstrap call did not reach ok (stage={first.stage!r}, "
        f"detail={first.detail!r}) — a real cluster must be able to complete it."
    )
    assert first.model_id

    from app.services.search import indexing_service

    # A hand-rolled counting spy, not unittest.mock.patch — this is a REAL cluster call
    # being counted, not a mocked-away one, and the audit-tests external-service-mock
    # detector cannot tell "wraps=" (a spy that still calls through) from a real
    # replacement. Save/restore the bound method manually instead.
    real_put_pipeline = indexing_service.opensearch_client.ingest.put_pipeline
    call_count = {"n": 0}

    def _counting_put_pipeline(*args: object, **kwargs: object):
        call_count["n"] += 1
        return real_put_pipeline(*args, **kwargs)

    indexing_service.opensearch_client.ingest.put_pipeline = _counting_put_pipeline
    try:
        second = ensure_neural_search_bootstrap()
    finally:
        indexing_service.opensearch_client.ingest.put_pipeline = real_put_pipeline

    assert second.state == "ok"
    assert second.model_id == first.model_id
    assert call_count["n"] == 0


def test_neural_search_ready_matches_the_real_probe():
    """The cheap probe used by every beat tick agrees with a real bootstrap outcome."""
    from app.core.config import settings
    from app.services.search.neural_bootstrap import ensure_neural_search_bootstrap
    from app.services.search.neural_bootstrap import neural_search_ready

    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        pytest.skip("Neural search disabled on this deployment")

    ensure_neural_search_bootstrap()
    assert neural_search_ready() is True
