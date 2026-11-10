"""SCIM paths for conditions a healthy deployment should not hit but must answer cleanly."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from app.api.endpoints.scim import groups
from app.api.endpoints.scim.errors import SCIMError
from app.api.endpoints.scim.errors import scim_error_handler
from app.models.scim_token import SCIMToken


def test_group_owner_requires_an_administrator_when_the_token_has_no_creator(monkeypatch):
    monkeypatch.setattr(groups, "get_first_super_admin", lambda _db: None)

    with pytest.raises(SCIMError) as caught:
        groups._owner_id(MagicMock(), SCIMToken(created_by=None))

    assert caught.value.status_code == 500
    assert "No administrator is available" in caught.value.detail


def test_error_handler_renders_a_foreign_exception_as_a_scim_500():
    response = asyncio.run(scim_error_handler(MagicMock(), RuntimeError("secret internals")))

    raw = bytes(response.body)
    body = json.loads(raw)
    assert response.status_code == 500
    assert body["detail"] == "Internal error"
    assert "secret" not in raw.decode()
