"""C10: every ``CATEGORY_SCHEMAS`` key must resolve to a real ``Settings`` attribute.

``AuthConfigService.env_var_for(key)`` is the fallback path ``get_effective_config``
uses when a key has no DB row yet: ``getattr(settings, env_var_for(key), None)``.
SAML worked only by coincidence — ``"saml_enabled".upper() == "SAML_ENABLED"`` for
every SAML key, so the unregistered fallback (``config_key.upper()``) happened to
land on a real attribute. That is not true of every category (``allow_registration``
-> ``ALLOW_OPEN_REGISTRATION`` is why ``ENV_TO_CONFIG_MAPPING`` exists at all), so an
unregistered key silently resolves to ``None`` the moment a rename breaks the
coincidence — this is a generic assertion so it catches the *next* provider too,
not just SAML.
"""

from __future__ import annotations

from app.core.config import settings
from app.schemas.auth_config import CATEGORY_SCHEMAS
from app.services.auth_config_service import AuthConfigService

#: Keys with no env-var backing at all — genuinely DB/admin-UI-only settings, not a
#: registration gap. Each entry needs a reason; a bare set here would be exactly the
#: kind of "silently exempted" the rest of this module argues against.
_DB_ONLY_KEYS = {
    "require_email_verification": "email-verification toggle, added DB-only, no .env twin",
    "pki_mode": "header vs mutual_tls selector, DB/UI-only",
    "pki_allow_password_fallback": "DB/UI-only PKI fallback toggle",
}


def test_every_category_schema_key_resolves_to_a_real_settings_attribute():
    missing = []
    for category, model in CATEGORY_SCHEMAS.items():
        for name in model.model_fields:
            if name in _DB_ONLY_KEYS:
                continue
            env_var = AuthConfigService.env_var_for(name)
            if not hasattr(settings, env_var):
                missing.append(f"{category}.{name} -> {env_var}")
    assert not missing, (
        "CATEGORY_SCHEMAS key(s) resolve to no Settings attribute via env_var_for(); "
        "register them in AuthConfigService.ENV_TO_CONFIG_MAPPING or add them to "
        "_DB_ONLY_KEYS above with a reason:\n" + "\n".join(missing)
    )


def test_a_deliberately_unmapped_key_still_fails():
    """Control: a key that resolves to no real attribute must be caught, so the
    assertion above cannot pass vacuously (e.g. from a bug that always finds
    ``missing`` empty)."""
    fake_key = "this_key_names_no_settings_attribute_at_all"
    env_var = AuthConfigService.env_var_for(fake_key)
    assert not hasattr(settings, env_var)


def test_every_saml_key_is_explicitly_registered_not_coincidental():
    """C10's actual fix: SAML keys must be REGISTERED, not merely resolvable by
    the upper-casing fallback. Deleting the SAML block from
    ``ENV_TO_CONFIG_MAPPING`` must make this fail even though the values would
    still coincidentally resolve via ``config_key.upper()``."""
    from app.schemas.auth_config import SAMLConfig

    assert len(SAMLConfig.model_fields) >= 15, "SAMLConfig looks emptied out — check the import"
    for name in SAMLConfig.model_fields:
        assert name in AuthConfigService.ENV_TO_CONFIG_MAPPING.values(), (
            f"saml.{name} is not explicitly registered in ENV_TO_CONFIG_MAPPING "
            "(it may still resolve by coincidence via config_key.upper())"
        )
