"""One host-identity rule for MediaCMS sources, and the public auth config's source scope.

A source configured as ``host:port`` must be recognised for URLs on that port (it was
never matched when only the bare hostname was compared on the client), the same rule must
serve every backend decision, and the "stored credentials" flag must reflect the viewer's
own and shared sources, not only system ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.protected_media_plugins.mediacms import MediacmsProvider
from app.services.protected_media_plugins.mediacms import host_matches

_VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "mediacms_host_vectors.json").read_text(
        encoding="utf-8"
    )
)["vectors"]

# Candidates a browser URL can never produce, so only the backend sees them.
_BACKEND_ONLY = [
    ("m.example.com", "evil@m.example.com", "https", False),
    ("m.example.com", "m.example.com:notaport", "https", False),
]


@pytest.mark.parametrize(
    ("configured", "candidate", "scheme", "expected"),
    [(v["configured"], v["candidate"], v["scheme"], v["expected"]) for v in _VECTORS]
    + _BACKEND_ONLY,
)
def test_one_identity_rule(configured, candidate, scheme, expected):
    assert host_matches(configured, candidate, scheme) is expected


def test_all_four_decisions_use_the_same_rule():
    provider = MediacmsProvider()
    sources = [
        {"hostname": "m.example.com:8443", "username": "u", "password": "p", "verify_ssl": False}
    ]
    with (
        patch.object(provider, "_get_all_sources", return_value=sources),
        patch.object(provider, "_get_db_sources", return_value=[]),
    ):
        assert provider.can_handle("https://m.example.com:8443/view?m=abc", user_id=1) is True
        assert provider.can_handle("https://m.example.com/view?m=abc", user_id=1) is False
        assert provider._get_stored_credentials(
            "m.example.com:8443", user_id=1, scheme="https"
        ) == (
            "u",
            "p",
        )
        assert provider._get_stored_credentials("m.example.com", user_id=1, scheme="https") == (
            None,
            None,
        )
        assert (
            provider._get_verify_ssl_for_host("m.example.com:8443", user_id=1, scheme="https")
            is False
        )


def test_public_auth_config_flags_the_viewers_own_source():
    provider = MediacmsProvider()
    own = {"hostname": "own.example.com", "username": "u", "password": "p"}
    with (
        patch.object(provider, "_get_user_media_sources", return_value=[own]),
        patch.object(provider, "_get_db_sources", return_value=[]),
    ):
        config = provider.get_public_auth_config(user_id=1)
    assert config["hosts_with_stored_credentials"] == ["own.example.com"]


def test_public_auth_config_does_not_flag_another_users_unshared_source():
    provider = MediacmsProvider()
    other = {"hostname": "theirs.example.com", "username": "u", "password": "p"}

    def _user_sources(user_id=None):
        return [other] if user_id == 2 else []

    with (
        patch.object(provider, "_get_user_media_sources", side_effect=_user_sources),
        patch.object(provider, "_get_db_sources", return_value=[]),
    ):
        mine = provider.get_public_auth_config(user_id=1)
        theirs = provider.get_public_auth_config(user_id=2)
    assert mine == {}, "user 1 sees neither the host nor a flag for user 2's source"
    assert theirs["hosts_with_stored_credentials"] == ["theirs.example.com"]
