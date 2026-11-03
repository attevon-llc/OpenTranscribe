"""A secret written over a row created before its key was sensitive must read back as a secret."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.auth_config import AuthConfig
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
