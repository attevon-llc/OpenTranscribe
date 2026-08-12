"""Functional tests for ``GET``/``PUT /api/admin/system/ai-summary`` (``admin.py``).

The deployment-wide kill switch for LLM summary generation had **no test of any
kind**: nothing in ``tests/`` issued a request to either verb, and the only other
mention of the path was ``unit/test_route_has_a_caller.py``. It is worth pinning
for three reasons:

* it is the switch a self-hoster uses to stop the app spending money at an LLM
  provider, so a PUT that shapes a response without persisting costs real money;
* the value is DB-backed ``SystemSettings`` (``ai.summary_enabled``, coded default
  in ``core/constants.py``) read by ``utils/summary_settings.py`` — a round trip
  is the only assertion that proves the write and the read agree on the key;
* ``enabled`` is a **bare ``bool`` after a keyword-only marker**, which makes it a
  *query* parameter, not a body field. A test that posted it as JSON would get a
  422 and could easily be "fixed" by giving the parameter a default — turning a
  malformed request into a silent deployment-wide disable.

``ai_summary_enabled`` is the *system* scope. The per-user twin lives at
``/api/user-settings/ai-summary`` and is covered by ``test_user_settings_routes.py``.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core.constants import DEFAULT_AI_SUMMARY_ENABLED
from app.models.system_settings import SystemSettings
from app.utils.summary_settings import is_summary_enabled_system

BASE = "/api/admin/system/ai-summary"
SETTING_KEY = "ai.summary_enabled"


@pytest.fixture
def clean_ai_summary_setting(db_session):
    """Remove the ambient row so the coded default is observable.

    The dev stack can carry an admin override from manual testing; the delete
    happens inside the test's savepoint and rolls back at teardown, so the live
    deployment's choice is restored.
    """
    db_session.query(SystemSettings).filter(SystemSettings.key == SETTING_KEY).delete(
        synchronize_session=False
    )
    db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES = [
    ("GET", BASE),
    ("PUT", f"{BASE}?enabled=false"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path):
    """A single account must not silence summarization for the whole deployment.

    Catches the dependency being relaxed to ``get_current_active_user``: any
    registered user could turn every other user's summaries off, and the failure
    would present as "the LLM stopped working" rather than as a settings change.
    """
    response = client.request(method, path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_every_route_requires_authentication(client, method, path):
    response = client.request(method, path)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_an_admin_does_not_need_super_admin(client, admin_token_headers, clean_ai_summary_setting):
    """This is the ``admin`` tier, not the super_admin one.

    The control for the refusal tests above: without it, a handler gated to
    ``get_current_active_superuser`` would pass every negative test here while
    403-ing the admins the panel is built for.
    """
    response = client.get(BASE, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def test_get_reports_the_coded_default_when_nothing_is_stored(
    client, admin_token_headers, clean_ai_summary_setting
):
    """An unconfigured deployment reports the coded default and says which scope.

    Catches the default flipping: summaries are billed per token at whatever
    provider the operator configured, so the direction of this default is a
    spending decision, not a cosmetic one.
    """
    response = client.get(BASE, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "ai_summary_enabled": DEFAULT_AI_SUMMARY_ENABLED,
        "scope": "system",
    }


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
def test_disabling_persists_and_is_read_back(
    client, admin_token_headers, clean_ai_summary_setting, db_session
):
    """PUT false → GET false, and the setting reader agrees.

    Three assertions on purpose: the PUT response, a fresh GET, and
    ``is_summary_enabled_system`` — the function the summarization pipeline
    actually calls. A handler that wrote a differently-spelled key would satisfy
    the first two (its own GET would read its own key) and leave the pipeline
    generating summaries the operator had switched off.
    """
    response = client.put(BASE, params={"enabled": False}, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"ai_summary_enabled": False, "scope": "system"}

    reread = client.get(BASE, headers=admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["ai_summary_enabled"] is False
    assert is_summary_enabled_system(db_session) is False


def test_re_enabling_persists_too(
    client, admin_token_headers, clean_ai_summary_setting, db_session
):
    """The control for the test above: the switch moves back.

    Without it, an implementation that stored a constant ``false`` — or that
    coerced any submitted value to False — would pass the disable round trip and
    make the feature impossible to turn back on from the UI.
    """
    assert (
        client.put(BASE, params={"enabled": False}, headers=admin_token_headers).status_code
        == status.HTTP_200_OK
    )

    response = client.put(BASE, params={"enabled": True}, headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["ai_summary_enabled"] is True

    reread = client.get(BASE, headers=admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["ai_summary_enabled"] is True
    assert is_summary_enabled_system(db_session) is True


def test_put_without_the_flag_is_a_422_and_changes_nothing(
    client, admin_token_headers, clean_ai_summary_setting, db_session
):
    """``enabled`` has no default, so an omitted value is refused.

    Catches a default being added to the signature. ``PUT`` with no payload is
    exactly what a broken frontend form sends, and if it defaulted to ``False``
    the deployment would disable summarization on a request that expressed no
    intention at all.
    """
    response = client.put(BASE, headers=admin_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert is_summary_enabled_system(db_session) is DEFAULT_AI_SUMMARY_ENABLED


def test_put_with_an_unparseable_flag_is_a_422_and_changes_nothing(
    client, admin_token_headers, clean_ai_summary_setting, db_session
):
    """A non-boolean is rejected rather than coerced.

    ``"maybe"`` must not become truthy. Pydantic's bool parsing accepts
    ``true/false/1/0/on/off``; anything else is an operator or client error, and
    coercing it would store a decision nobody made.
    """
    response = client.put(BASE, params={"enabled": "maybe"}, headers=admin_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert is_summary_enabled_system(db_session) is DEFAULT_AI_SUMMARY_ENABLED
