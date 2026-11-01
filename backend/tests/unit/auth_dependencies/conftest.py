"""Fixtures for the ``auth_dependencies`` survivor-mutant suites."""

import pytest

from app.auth.provider_registry import unregister_verifier
from tests.unit.auth_dependencies._helpers import PROVIDER


@pytest.fixture(autouse=True)
def _clean_registry():
    """This module's community build registers no external verifiers."""
    unregister_verifier(PROVIDER)
    yield
    unregister_verifier(PROVIDER)
