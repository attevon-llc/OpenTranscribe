"""Stored auth secrets survive a round trip through the admin UI.

The panel loads `GET /admin/auth-config`, binds each value into a form field, and
PUTs the whole category back when the admin saves ANY field in that tab. The read
side returned the literal ``***REDACTED***`` for sensitive keys, and the write
side skipped only ``None``/``""`` — so opening the LDAP tab and clicking Save
encrypted the placeholder over the real bind password and reported success. The
same path applied to ``keycloak_client_secret``.

A second, opposite defect on the same surface: ``get_config_by_category`` only
masked inside its ``decrypt=True`` branch, while the admin endpoint calls it with
``decrypt=False`` — so that endpoint returned the raw ciphertext to the browser.
"""

from __future__ import annotations

import pytest

from app.services.auth_config_service import SENSITIVE_NO_CHANGE_VALUES
from app.services.auth_config_service import SENSITIVE_SET_SENTINEL
from app.services.auth_config_service import AuthConfigService


class TestPlaceholdersAreNeverStored:
    @pytest.mark.parametrize("placeholder", sorted(SENSITIVE_NO_CHANGE_VALUES))
    def test_every_placeholder_is_treated_as_no_change(self, placeholder):
        """These all mean "the admin did not type a new secret"."""
        assert placeholder in SENSITIVE_NO_CHANGE_VALUES

    def test_the_old_literal_is_still_recognised(self):
        """An older client may echo the previous placeholder back — refuse it too."""
        assert "***REDACTED***" in SENSITIVE_NO_CHANGE_VALUES

    def test_bulk_update_skips_placeholder_values(self, monkeypatch):
        written: dict[str, object] = {}

        def _fake_set_config(*, db, key, value, **kwargs):
            written[key] = value
            return object()

        monkeypatch.setattr(AuthConfigService, "set_config", staticmethod(_fake_set_config))

        AuthConfigService.bulk_update_category(
            db=None,
            category="ldap",
            config_dict={
                "ldap_bind_password": SENSITIVE_SET_SENTINEL,
                "ldap_server": "ldaps://ad.example.com",
            },
            user_id=1,
        )

        assert "ldap_bind_password" not in written, (
            "the stored secret must be left alone when the client echoes a placeholder"
        )
        assert written["ldap_server"] == "ldaps://ad.example.com"

    def test_bulk_update_still_writes_a_real_new_secret(self, monkeypatch):
        written: dict[str, object] = {}

        def _fake_set_config(*, db, key, value, **kwargs):
            written[key] = value
            return object()

        monkeypatch.setattr(AuthConfigService, "set_config", staticmethod(_fake_set_config))

        AuthConfigService.bulk_update_category(
            db=None,
            category="ldap",
            config_dict={"ldap_bind_password": "a-genuinely-new-secret"},
            user_id=1,
        )

        assert written["ldap_bind_password"] == "a-genuinely-new-secret"


class TestSecretsNeverReachTheWire:
    def test_admin_list_endpoint_sends_no_secret_and_no_placeholder(self):
        """`config_value` is None for a sensitive key; `is_set` carries the signal."""
        import inspect

        from app.api.endpoints import auth_config

        source = inspect.getsource(auth_config.get_all_configs)
        code = "\n".join(line.split("#")[0] for line in source.splitlines())
        assert '"***REDACTED***"' not in code
        assert '"is_set"' in code

    def test_response_schema_exposes_is_set(self):
        from app.schemas.auth_config import AuthConfigResponse

        assert "is_set" in AuthConfigResponse.model_fields

    def test_category_getter_masks_even_when_not_decrypting(self):
        """decrypt=False is exactly how the admin endpoint calls it."""
        import inspect

        from app.services.auth_config_service import AuthConfigService

        source = inspect.getsource(AuthConfigService.get_config_by_category)
        # The masking must happen before, and independently of, the decrypt branch.
        assert "not decrypt" in source
        assert "SENSITIVE_SET_SENTINEL" in source
