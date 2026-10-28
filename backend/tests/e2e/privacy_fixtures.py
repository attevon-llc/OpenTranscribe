"""Fixtures shared by the privacy journeys (issue #1335), registered in ``conftest.py``.

They act on the throwaway ``second_user`` only, so a failed restore can never damage the
shared admin account. Each state-changing fixture restores in a ``finally``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import requests
from privacy_ui import token_for


@pytest.fixture(scope="module")
def user_headers(backend_url: str, second_user: dict[str, str]) -> dict[str, str]:
    """Bearer headers for the throwaway account, for reading and restoring state."""
    token = token_for(backend_url, second_user["email"], second_user["password"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def voiceprints_restored(backend_url: str, user_headers: dict[str, str]) -> Iterator[None]:
    """Whatever the test did, the account leaves with voiceprint matching on."""
    try:
        yield
    finally:
        requests.put(
            f"{backend_url}/api/privacy/me",
            headers=user_headers,
            json={"voiceprint_matching_enabled": True},
            timeout=30,
        )


@pytest.fixture
def gender_restored(backend_url: str, user_headers: dict[str, str]) -> Iterator[None]:
    """Whatever the test did, the account leaves with gender prediction on."""
    try:
        yield
    finally:
        requests.put(
            f"{backend_url}/api/user-settings/speaker-attributes",
            headers=user_headers,
            json={"gender_detection_enabled": True},
            timeout=30,
        )


@pytest.fixture
def voiceprints_off(
    backend_url: str, user_headers: dict[str, str], voiceprints_restored: None
) -> None:
    """The account starts the test with voiceprint matching OFF (restored afterwards)."""
    response = requests.put(
        f"{backend_url}/api/privacy/me",
        headers=user_headers,
        json={"voiceprint_matching_enabled": False},
        timeout=30,
    )
    assert response.status_code == 200, response.text
