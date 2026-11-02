"""A secret written over a row created before its key was sensitive must read back as a secret."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.auth_config import AuthConfig
from app.services.auth_config_service import SENSITIVE_SET_SENTINEL
from app.services.auth_config_service import AuthConfigService

_ENCRYPT = "app.services.auth_config_service.encrypt_api_key"
_DECRYPT = "app.services.auth_config_service.decrypt_api_key"


def _legacy_row() -> AuthConfig:
    return AuthConfig(
        config_key="proxy_shared_secret",
        config_value="old-plaintext",
        is_sensitive=False,
        category="proxy",
        data_type="string",
    )


def test_rewriting_a_legacy_row_marks_it_sensitive() -> None:
    row = _legacy_row()
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.first.return_value = row

    with patch(_ENCRYPT, return_value="ciphertext"):
        AuthConfigService.set_config(
            db, "proxy_shared_secret", "new-secret", is_sensitive=True, category="proxy", user_id=1
        )

    assert row.config_value == "ciphertext"
    assert row.is_sensitive is True


def test_a_rewritten_legacy_secret_is_decrypted_on_read_not_served_as_ciphertext() -> None:
    row = _legacy_row()
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.first.return_value = row

    with patch(_ENCRYPT, return_value="ciphertext"):
        AuthConfigService.set_config(
            db, "proxy_shared_secret", "new-secret", is_sensitive=True, category="proxy", user_id=1
        )
    with patch(_DECRYPT, return_value="new-secret"):
        assert AuthConfigService.get_config(db, "proxy_shared_secret") == "new-secret"


def _unflagged_ciphertext_row(key: str = "proxy_shared_secret") -> AuthConfig:
    # The shape a pre-fix rewrite left behind: encrypted value, flag still False.
    return AuthConfig(
        config_key=key,
        config_value="ciphertext",
        is_sensitive=False,
        category="proxy",
        data_type="string",
    )


def _db_first(row: AuthConfig) -> MagicMock:
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.first.return_value = row
    return db


def _db_all(rows: list[AuthConfig]) -> MagicMock:
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


def test_get_config_decrypts_a_listed_key_even_when_its_row_is_unflagged() -> None:
    db = _db_first(_unflagged_ciphertext_row())
    with patch(_DECRYPT, return_value="the-secret") as decrypt:
        assert AuthConfigService.get_config(db, "proxy_shared_secret") == "the-secret"
    decrypt.assert_called_once_with("ciphertext")


def test_get_config_undecryptable_unflagged_listed_key_reads_as_unset() -> None:
    db = _db_first(_unflagged_ciphertext_row())
    with patch(_DECRYPT, side_effect=Exception("bad key")):
        assert AuthConfigService.get_config(db, "proxy_shared_secret") is None


def test_category_read_masks_a_listed_key_even_when_its_row_is_unflagged() -> None:
    db = _db_all([_unflagged_ciphertext_row()])
    with patch(_DECRYPT) as decrypt:
        result = AuthConfigService.get_config_by_category(db, "proxy", decrypt=False)
    assert result == {"proxy_shared_secret": SENSITIVE_SET_SENTINEL}
    decrypt.assert_not_called()


def test_category_decrypted_read_decrypts_an_unflagged_listed_key() -> None:
    db = _db_all([_unflagged_ciphertext_row()])
    with patch(_DECRYPT, return_value="the-secret"):
        result = AuthConfigService.get_config_by_category(db, "proxy", decrypt=True)
    assert result == {"proxy_shared_secret": "the-secret"}


def test_an_unlisted_unflagged_key_is_served_as_stored() -> None:
    row = AuthConfig(
        config_key="proxy_user_header",
        config_value="X-Forwarded-Email",
        is_sensitive=False,
        category="proxy",
        data_type="string",
    )
    with patch(_DECRYPT) as decrypt:
        assert AuthConfigService.get_config(_db_first(row), "proxy_user_header") == (
            "X-Forwarded-Email"
        )
        assert AuthConfigService.get_config_by_category(_db_all([row]), "proxy", decrypt=False) == {
            "proxy_user_header": "X-Forwarded-Email"
        }
    decrypt.assert_not_called()
