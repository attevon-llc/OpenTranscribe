"""Functional tests for the retry + garbage-cleanup admin settings (``admin.py``).

``GET``/``PUT /api/admin/settings/retry-config`` and
``GET``/``PUT /api/admin/settings/garbage-cleanup`` had **no functional
coverage** — no test in the tree issued a request to any of the four. They are
DB-backed ``SystemSettings`` with coded defaults, and both feed the transcription
pipeline rather than a display: ``retry_limit_enabled``/``max_retries`` decide
whether a failing file is retried forever, and ``max_word_length`` decides which
transcript words get replaced as garbage.

The invariants pinned here:

* the tier is ``admin``; a plain user gets 403 and an anonymous caller 401;
* the coded defaults are what an unconfigured deployment reports;
* a partial patch writes only the field it names — the other survives;
* out-of-range values are refused **and not persisted** (a stored
  ``max_word_length`` of 0 would replace every word in every transcript).
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models.system_settings import SystemSettings
from app.services import system_settings_service as sss

RETRY = "/api/admin/settings/retry-config"
GARBAGE = "/api/admin/settings/garbage-cleanup"

_KEYS = [
    "transcription.max_retries",
    "transcription.retry_limit_enabled",
    "transcription.garbage_cleanup_enabled",
    "transcription.max_word_length",
]


@pytest.fixture
def clean_transcription_settings(db_session):
    """Remove ambient rows for these four keys so coded defaults are observable.

    The dev stack can carry admin overrides from manual testing; the deletion
    happens inside the test's savepoint and rolls back at teardown.
    """
    db_session.query(SystemSettings).filter(SystemSettings.key.in_(_KEYS)).delete(
        synchronize_session=False
    )
    db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES = [
    ("GET", RETRY, None),
    ("PUT", RETRY, {"max_retries": 5}),
    ("GET", GARBAGE, None),
    ("PUT", GARBAGE, {"max_word_length": 60}),
]


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path, body):
    """These are deployment-wide pipeline settings, not per-user preferences.

    Catches the dependency being relaxed to ``get_current_active_user``: any
    account could disable the retry limit deployment-wide, which turns one
    permanently failing file into an unbounded GPU-worker retry loop.
    """
    response = client.request(method, path, headers=user_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_every_route_requires_authentication(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------
def test_retry_get_returns_the_coded_defaults(
    client, admin_token_headers, clean_transcription_settings
):
    """An unconfigured deployment enforces a bounded retry count.

    Catches ``retry_limit_enabled``'s default flipping to False, which would make
    a file that fails deterministically requeue forever and occupy the single GPU
    worker indefinitely.
    """
    response = client.get(RETRY, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["max_retries"] == 3
    assert body["retry_limit_enabled"] is True


def test_retry_put_writes_only_the_named_field(
    client, admin_token_headers, clean_transcription_settings
):
    """Patching the count must not silently switch enforcement off.

    Catches ``None`` being written through for the unmentioned field — the UI
    submits one control at a time, so a write-through would disable the limit as a
    side effect of raising the count.
    """
    response = client.put(RETRY, headers=admin_token_headers, json={"max_retries": 7})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["max_retries"] == 7
    assert body["retry_limit_enabled"] is True

    reread = client.get(RETRY, headers=admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["max_retries"] == 7


def test_retry_put_can_disable_the_limit(client, admin_token_headers, clean_transcription_settings):
    """The control for the test above: the flag really is settable.

    Without it, a handler that ignored ``retry_limit_enabled`` entirely would pass
    the "unchanged" assertion above.
    """
    response = client.put(RETRY, headers=admin_token_headers, json={"retry_limit_enabled": False})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["retry_limit_enabled"] is False
    assert response.json()["max_retries"] == 3


@pytest.mark.parametrize("value", [-1, 100])
def test_retry_put_out_of_range_is_a_422_and_is_not_persisted(
    client, admin_token_headers, clean_transcription_settings, db_session, value
):
    """``RetryConfigUpdate`` bounds ``max_retries`` to 0-99, so nothing is written."""
    response = client.put(RETRY, headers=admin_token_headers, json={"max_retries": value})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert sss.get_retry_config(db_session)["max_retries"] == 3


def test_retry_zero_means_unlimited_and_is_accepted(
    client, admin_token_headers, clean_transcription_settings
):
    """Zero is the documented "unlimited" sentinel, not an out-of-range value.

    Catches a bound tightened to ``> 0``: the only way to express "never give up"
    would disappear, and the validator's own message would then contradict itself.
    """
    response = client.put(RETRY, headers=admin_token_headers, json={"max_retries": 0})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["max_retries"] == 0


# ---------------------------------------------------------------------------
# Garbage cleanup
# ---------------------------------------------------------------------------
def test_garbage_get_returns_the_coded_defaults(
    client, admin_token_headers, clean_transcription_settings
):
    response = client.get(GARBAGE, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["garbage_cleanup_enabled"] is True
    assert body["max_word_length"] == 50


def test_garbage_put_writes_only_the_named_field(
    client, admin_token_headers, clean_transcription_settings
):
    response = client.put(GARBAGE, headers=admin_token_headers, json={"max_word_length": 120})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["max_word_length"] == 120
    assert body["garbage_cleanup_enabled"] is True

    reread = client.get(GARBAGE, headers=admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["max_word_length"] == 120


def test_garbage_put_can_disable_cleanup(client, admin_token_headers, clean_transcription_settings):
    """The control for the test above: the flag really is settable."""
    response = client.put(
        GARBAGE, headers=admin_token_headers, json={"garbage_cleanup_enabled": False}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["garbage_cleanup_enabled"] is False
    assert response.json()["max_word_length"] == 50


@pytest.mark.parametrize("value", [0, 19, 201])
def test_garbage_put_out_of_range_is_a_422_and_is_not_persisted(
    client, admin_token_headers, clean_transcription_settings, db_session, value
):
    """The 20-200 band is enforced at the wire.

    ``0`` is the destructive case: the threshold is "words longer than this are
    replaced", so a stored zero would blank out every word of every transcript
    produced afterwards. The non-persistence assertion catches validation moving
    to *after* the write.
    """
    response = client.put(GARBAGE, headers=admin_token_headers, json={"max_word_length": value})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert sss.get_garbage_cleanup_config(db_session)["max_word_length"] == 50
