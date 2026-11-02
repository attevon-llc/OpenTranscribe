"""Shared fixtures for the PKI authentication tests."""

from unittest.mock import MagicMock

import pytest

from tests.fixtures.pki_auth import generate_test_certificate


@pytest.fixture(autouse=True)
def _isolate_process_auth_settings():
    """Keep one test's published PKI settings out of the next one's."""
    from app.core.process_auth_settings import clear_process_auth_settings_cache

    clear_process_auth_settings_cache()
    yield
    clear_process_auth_settings_cache()


# ===== Test Fixtures =====


@pytest.fixture
def valid_certificate_pem():
    """Generate a valid test certificate (valid for 1 year from now)."""
    return generate_test_certificate(
        common_name="Test User",
        email="test.user@example.com",
        organization="Test Organization",
        organizational_unit="Test Unit",
        days_valid=365,
        days_before=0,
    )


@pytest.fixture
def expired_certificate_pem():
    """Generate an expired test certificate (expired 1 day ago)."""
    return generate_test_certificate(
        common_name="Expired User",
        email="expired@example.com",
        organization="Test Organization",
        organizational_unit="Test Unit",
        days_valid=-1,  # Expired 1 day ago
        days_before=30,  # Was valid starting 30 days ago
    )


@pytest.fixture
def not_yet_valid_certificate_pem():
    """Generate a certificate that's not yet valid (valid starting tomorrow)."""
    return generate_test_certificate(
        common_name="Future User",
        email="future@example.com",
        organization="Test Organization",
        organizational_unit="Test Unit",
        days_valid=365,
        days_before=-1,  # Starts tomorrow
    )


@pytest.fixture
def dod_cac_certificate_pem():
    """Generate a test certificate with DoD CAC-style DN."""
    return generate_test_certificate(
        common_name="SMITH.JOHN.WILLIAM.1234567890",
        email=None,
        organization="U.S. Government",
        organizational_unit="DoD",
        days_valid=365,
        days_before=0,
    )


@pytest.fixture
def piv_certificate_pem():
    """Generate a test certificate with PIV-style DN."""
    return generate_test_certificate(
        common_name="JONES.JANE.M",
        email="jane.jones@agency.gov",
        organization="U.S. Government",
        organizational_unit="Federal Agency",
        days_valid=365,
        days_before=0,
    )


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request object."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "192.168.1.100"
    request.headers = {}
    return request


@pytest.fixture
def mock_trusted_request():
    """Create a mock request from a trusted proxy IP."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "10.0.0.1"
    request.headers = {}
    return request
