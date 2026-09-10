"""Issue #893 — ``OPENSEARCH_EMBEDDING_MODE`` had no validation.

Before this, ``Settings.OPENSEARCH_EMBEDDING_MODE`` was a plain ``str`` compared at exactly one
site (``services/search/neural_bootstrap.py::_managed_embedding_mode``) with a single
``== "managed"``. Any other value — including a plausible typo like ``"app"`` — silently
resolved to the "local" branch with no error. On a self-hosted cluster that still works; on a
MANAGED OpenSearch domain the local bootstrap's cluster-settings mutation is rejected, so
semantic search silently returns nothing while keyword search keeps working — invisible short of
a query-by-query comparison.

``Settings._validate_embedding_mode`` now refuses construction outright (a pydantic
``ValidationError``) for any value outside ``{"local", "managed"}``, matching the existing
``assemble_cors_origins`` pattern for a bad ``CORS_ORIGINS``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_an_unknown_embedding_mode_is_refused():
    """RED before the fix: this constructed fine and OPENSEARCH_EMBEDDING_MODE=="app"."""
    with pytest.raises(ValidationError, match="OPENSEARCH_EMBEDDING_MODE"):
        Settings(OPENSEARCH_EMBEDDING_MODE="app")


def test_an_empty_embedding_mode_is_refused():
    with pytest.raises(ValidationError, match="OPENSEARCH_EMBEDDING_MODE"):
        Settings(OPENSEARCH_EMBEDDING_MODE="")


@pytest.mark.parametrize("value", ["local", "managed"])
def test_valid_modes_round_trip_unchanged(value):
    assert value == Settings(OPENSEARCH_EMBEDDING_MODE=value).OPENSEARCH_EMBEDDING_MODE


def test_a_mode_is_normalized_by_strip_and_lower():
    """Matches the comparison site's own normalization, so a trailing-space or
    differently-cased value is accepted rather than rejected on a technicality."""
    assert Settings(OPENSEARCH_EMBEDDING_MODE="MANAGED ").OPENSEARCH_EMBEDDING_MODE == "managed"
    assert Settings(OPENSEARCH_EMBEDDING_MODE=" Local").OPENSEARCH_EMBEDDING_MODE == "local"


def test_the_default_is_still_local():
    assert Settings().OPENSEARCH_EMBEDDING_MODE == "local"


def test_managed_embedding_mode_resolution_has_no_second_normalization(monkeypatch):
    """`_managed_embedding_mode` must not re-strip/lower — the validator already
    normalized at construction, and duplicating it is how the two drift apart."""
    from app.core.config import settings
    from app.services.search.neural_bootstrap import _managed_embedding_mode

    monkeypatch.setattr(settings, "OPENSEARCH_EMBEDDING_MODE", "managed")
    assert _managed_embedding_mode() is True

    monkeypatch.setattr(settings, "OPENSEARCH_EMBEDDING_MODE", "local")
    assert _managed_embedding_mode() is False
