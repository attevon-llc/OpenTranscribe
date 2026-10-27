"""Pinning the local Whisper model is a ``super_admin`` act, not a plain admin one.

``/asr-settings/local-model/set`` and ``/restart`` change what every tenant's transcription
runs on and send a shutdown to the shared GPU workers, so a plain ``admin`` is refused in every
deployment (the reading route, ``/local-model/active``, is ordinary deployment metadata).

``/restart`` is checked by its dependency tree rather than by calling it: a regression there
must fail this test, not broadcast a shutdown to the dev stack's real workers.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fixtures.platform_access_matrix import dependency_names

from app.main import app

PREFIX = "/api/asr-settings/local-model"


def _route(path: str) -> APIRoute:
    return next(
        r for r in app.routes if isinstance(r, APIRoute) and r.path == path and "POST" in r.methods
    )


@pytest.mark.parametrize("path", [f"{PREFIX}/set", f"{PREFIX}/restart"])
def test_the_route_depends_on_the_super_admin_gate_and_not_the_admin_one(path):
    names = dependency_names(_route(path).dependant)
    assert "get_current_active_superuser" in names
    assert "get_current_admin_user" not in names


def test_a_plain_admin_cannot_set_the_local_model(client, admin_token_headers, db_session):
    refused = client.post(
        f"{PREFIX}/set", json={"model_name": "large-v3"}, headers=admin_token_headers
    )
    assert refused.status_code == 403, refused.text


def test_a_super_admin_passes_the_gate(client, super_admin_token_headers):
    """The control: a super_admin gets past the gate to validation (400), not a 403."""
    passed = client.post(
        f"{PREFIX}/set", json={"model_name": "  "}, headers=super_admin_token_headers
    )
    assert passed.status_code == 400, passed.text
