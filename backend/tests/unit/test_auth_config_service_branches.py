"""Pins for AuthConfigService branches no other test could tell apart from a mutant.

Each test here was written against a deliberate mutation of
``app/services/auth_config_service.py`` that the rest of the auth-config suite let
pass (secret masking on read, the "leave the stored secret alone" write skip,
audit redaction on update, strict boolean parsing, schema-default fallbacks and the
cross-field state the joint validation rules see).
"""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models.auth_config import AuthConfig
from app.models.auth_config import AuthConfigAudit
from app.schemas.auth_config import coded_default
from app.services.auth_config_service import SENSITIVE_SET_SENTINEL
from app.services.auth_config_service import AuthConfigService

# Every test here may insert auth_config rows (migrate_from_env seeds every absent key) in an
# uncommitted transaction; two workers doing that deadlock on auth_config_config_key_key.
# One group per overlapping key namespace keeps them on one worker (backend/tests/CLAUDE.md).
pytestmark = pytest.mark.xdist_group("auth_config")

_DECRYPT = "app.services.auth_config_service.decrypt_api_key"


def _row(key: str, value: str | None, *, sensitive: bool, data_type: str = "string") -> AuthConfig:
    return AuthConfig(
        config_key=key,
        config_value=value,
        is_sensitive=sensitive,
        category="ldap",
        data_type=data_type,
    )


def _db_with_rows(rows: list[AuthConfig]) -> MagicMock:
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


class TestGetConfigEmptyDecrypt:
    def test_empty_string_from_decrypt_is_unset_not_empty(self) -> None:
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = _row(
            "ldap_bind_password", "ciphertext", sensitive=True
        )
        with patch(_DECRYPT, return_value=""):
            assert AuthConfigService.get_config(db, "ldap_bind_password") is None


class TestCategoryReadMasksSecrets:
    def test_undecrypted_read_returns_sentinel_never_ciphertext(self) -> None:
        db = _db_with_rows([_row("ldap_bind_password", "ciphertext", sensitive=True)])
        with patch(_DECRYPT) as decrypt:
            result = AuthConfigService.get_config_by_category(db, "ldap", decrypt=False)
        assert result == {"ldap_bind_password": SENSITIVE_SET_SENTINEL}
        decrypt.assert_not_called()

    def test_unset_secret_reads_as_none_not_sentinel(self) -> None:
        db = _db_with_rows([_row("ldap_bind_password", None, sensitive=True)])
        result = AuthConfigService.get_config_by_category(db, "ldap", decrypt=False)
        assert result == {"ldap_bind_password": None}

    # Same answer as get_config: undecryptable is unset, never a credential-shaped
    # placeholder a caller could use as the secret.
    def test_decrypt_failure_reads_as_unset(self) -> None:
        db = _db_with_rows([_row("ldap_bind_password", "ciphertext", sensitive=True)])
        with patch(_DECRYPT, side_effect=Exception("bad key")):
            result = AuthConfigService.get_config_by_category(db, "ldap", decrypt=True)
        assert result == {"ldap_bind_password": None}

    def test_empty_decrypt_reads_as_unset(self) -> None:
        db = _db_with_rows([_row("ldap_bind_password", "ciphertext", sensitive=True)])
        with patch(_DECRYPT, return_value=""):
            result = AuthConfigService.get_config_by_category(db, "ldap", decrypt=True)
        assert result == {"ldap_bind_password": None}

    def test_decrypted_secret_is_returned(self) -> None:
        db = _db_with_rows([_row("ldap_bind_password", "ciphertext", sensitive=True)])
        with patch(_DECRYPT, return_value="the-secret"):
            result = AuthConfigService.get_config_by_category(db, "ldap", decrypt=True)
        assert result == {"ldap_bind_password": "the-secret"}

    def test_retired_key_row_is_not_served(self) -> None:
        db = _db_with_rows(
            [
                _row("max_login_attempts", "5", sensitive=False, data_type="int"),
                _row("ldap_server", "ldap.example", sensitive=False),
            ]
        )
        result = AuthConfigService.get_config_by_category(db, "local", decrypt=False)
        assert result == {"ldap_server": "ldap.example"}


class TestConvertValueFallbacks:
    def test_malformed_int_uses_schema_default_not_zero(self) -> None:
        expected = coded_default("ldap_port")
        assert expected not in (None, 0)
        assert AuthConfigService._convert_value("63x", "int", "ldap_port") == expected

    @pytest.mark.parametrize(
        ("raw", "expected"), [(" false ", False), ("\tFALSE\n", False), (" true ", True)]
    )
    def test_bool_ignores_surrounding_whitespace(self, raw: str, expected: bool) -> None:
        # oidc_verify_issuer defaults True, so " false " only reads False if parsed.
        assert coded_default("oidc_verify_issuer") is True
        assert AuthConfigService._convert_value(raw, "bool", "oidc_verify_issuer") is expected


class TestSetConfigAuditRedaction:
    def test_update_of_sensitive_row_redacts_old_value(self) -> None:
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = _row(
            "ldap_bind_password", "old-ciphertext", sensitive=True
        )
        with (
            patch("app.services.auth_config_service.encrypt_api_key", return_value="new-ct"),
            patch("app.services.auth_config_service.prime_process_auth_settings"),
        ):
            AuthConfigService.set_config(
                db,
                key="ldap_bind_password",
                value="new-secret",
                is_sensitive=True,
                category="ldap",
                user_id=1,
            )
        audits = [
            c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuthConfigAudit)
        ]
        assert len(audits) == 1
        assert audits[0].old_value == "***REDACTED***"
        assert audits[0].new_value == "***REDACTED***"


class TestBulkUpdateSkipsUntypedSecret:
    def test_none_for_sensitive_key_never_reaches_set_config(self) -> None:
        def passthrough(_category: str, payload: dict[str, Any], current: Any) -> dict[str, Any]:
            return payload

        db = MagicMock(spec=Session)
        with (
            patch("app.services.auth_config_service.validate_category_config", passthrough),
            patch.object(AuthConfigService, "_cross_field_state", return_value={}),
            patch.object(AuthConfigService, "set_config") as set_config,
        ):
            result = AuthConfigService.bulk_update_category(
                db, "ldap", {"ldap_bind_password": None}, user_id=1
            )
        assert result == {}
        set_config.assert_not_called()


class TestCrossFieldState:
    def test_payload_keys_are_not_reloaded(self) -> None:
        db = MagicMock(spec=Session)
        with patch.object(AuthConfigService, "get_effective_config", return_value=False) as eff:
            state = AuthConfigService._cross_field_state(db, "local", {"local_enabled": True})
        assert state == {"allow_registration": False}
        assert [c.args[1] for c in eff.call_args_list] == ["allow_registration"]

    def test_unresolved_key_falls_back_to_coded_default(self) -> None:
        db = MagicMock(spec=Session)
        with patch.object(AuthConfigService, "get_effective_config", return_value=None):
            state = AuthConfigService._cross_field_state(db, "password_policy", {})
        assert state == {
            "password_min_length": coded_default("password_min_length"),
            "password_max_length": coded_default("password_max_length"),
        }
        assert None not in state.values()


_PLACEHOLDERS = (SENSITIVE_SET_SENTINEL, "***REDACTED***", "***ENCRYPTED***")


class TestSetConfigRefusesPlaceholderSecret:
    @pytest.mark.parametrize("placeholder", _PLACEHOLDERS)
    @pytest.mark.parametrize("flagged", [True, False], ids=["flagged", "key_listed_only"])
    def test_placeholder_is_refused_before_anything_is_written(
        self, placeholder: str, flagged: bool
    ) -> None:
        db = MagicMock(spec=Session)
        with (
            patch("app.services.auth_config_service.encrypt_api_key") as encrypt,
            patch("app.services.auth_config_service.prime_process_auth_settings"),
            pytest.raises(ValueError, match="ldap_bind_password"),
        ):
            AuthConfigService.set_config(
                db,
                key="ldap_bind_password",
                value=placeholder,
                is_sensitive=flagged,
                category="ldap",
                user_id=1,
            )
        encrypt.assert_not_called()
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_refusal_is_a_value_error_so_the_update_route_answers_400(self) -> None:
        from app.services.auth_config_service import PlaceholderSecretError

        assert issubclass(PlaceholderSecretError, ValueError)

    def test_a_real_secret_is_still_encrypted_and_written(self) -> None:
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        with (
            patch("app.services.auth_config_service.encrypt_api_key", return_value="ct") as enc,
            patch("app.services.auth_config_service.prime_process_auth_settings"),
        ):
            row = AuthConfigService.set_config(
                db,
                key="ldap_bind_password",
                value="a-real-secret",
                is_sensitive=True,
                category="ldap",
                user_id=1,
            )
        enc.assert_called_once_with("a-real-secret")
        assert row.config_value == "ct"
        db.commit.assert_called_once()

    def test_a_placeholder_shaped_value_is_stored_for_a_non_secret_key(self) -> None:
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.services.auth_config_service.prime_process_auth_settings"):
            row = AuthConfigService.set_config(
                db,
                key="ldap_server",
                value=SENSITIVE_SET_SENTINEL,
                is_sensitive=False,
                category="ldap",
                user_id=1,
            )
        assert row.config_value == SENSITIVE_SET_SENTINEL


class TestMigrateFromEnvSkipsPlaceholderSecret:
    def test_placeholder_env_secret_is_not_migrated_and_the_rest_are(self, monkeypatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(
            settings, AuthConfigService.env_var_for("ldap_bind_password"), SENSITIVE_SET_SENTINEL
        )
        monkeypatch.setattr(
            settings, AuthConfigService.env_var_for("ldap_server"), "ldaps://ad.example"
        )
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.first.return_value = None
        with (
            patch("app.services.auth_config_service.encrypt_api_key", return_value="ct") as enc,
            patch("app.services.auth_config_service.prime_process_auth_settings"),
        ):
            count = AuthConfigService.migrate_from_env(db, user_id=1)

        written = {
            c.args[0].config_key: c.args[0].config_value
            for c in db.add.call_args_list
            if isinstance(c.args[0], AuthConfig)
        }
        assert "ldap_bind_password" not in written
        assert SENSITIVE_SET_SENTINEL not in [c.args[0] for c in enc.call_args_list]
        assert written["ldap_server"] == "ldaps://ad.example"
        assert count == len(written)
