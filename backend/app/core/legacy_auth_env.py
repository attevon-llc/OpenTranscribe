"""The one place the retired vendor spelling of the OIDC settings survives.

OpenTranscribe's OIDC surface is named ``oidc_*`` everywhere: config keys, Pydantic
schema, service, routes, admin panel, i18n. Deployments, however, own their ``.env``
file — the repo's own rule is that it "is never overwritten without confirmation" —
so silently dropping the historical ``KEYCLOAK_*`` variable names would disable SSO
on upgrade for every existing installation.

This module is therefore an **input adapter**, not a second implementation. It
translates one set of environment-variable *names* into the canonical ones before
``Settings`` is built, and then nothing downstream ever sees the old spelling:
``app/core/config.py`` declares only ``OIDC_*`` attributes, and
``AuthConfigService.ENV_TO_CONFIG_MAPPING`` maps only ``OIDC_*`` env names onto
``oidc_*`` config keys. That distinction is what keeps the repo's "if you replace an
implementation, delete the old one" rule honest, and
``tests/unit/test_oidc_naming_invariant.py`` enforces it: this file is one of exactly
two under ``backend/app/`` permitted to name the old provider at all.

Precedence
----------
The legacy name **wins** when both spellings are set. That preserves the behaviour
documented before the rename (``KEYCLOAK_DISCOVERY_URL`` beat ``OIDC_DISCOVERY_URL``)
and means an upgrade can add ``OIDC_*`` to a template without changing what a
deployment that still sets ``KEYCLOAK_*`` resolves to.

Removal
-------
None planned. Renaming a user-owned file is not a thing this project gets to do, so
these aliases stay. What is bounded is the *noise*: :func:`deprecated_oidc_env_names`
drives a single startup log line so operators know the canonical spelling exists.
"""

import os
from collections.abc import Mapping

#: Canonical ``OIDC_*`` environment name -> the historical spelling it replaced.
#:
#: Only names that ``Settings`` actually declares belong here. The test-container
#: variables (the admin account and port of the local Keycloak used by
#: ``--with-keycloak-test``) are not application settings and are deliberately absent.
LEGACY_OIDC_ENV_ALIASES: dict[str, str] = {
    "OIDC_ENABLED": "KEYCLOAK_ENABLED",
    "OIDC_SERVER_URL": "KEYCLOAK_SERVER_URL",
    "OIDC_INTERNAL_URL": "KEYCLOAK_INTERNAL_URL",
    "OIDC_REALM": "KEYCLOAK_REALM",
    "OIDC_CLIENT_ID": "KEYCLOAK_CLIENT_ID",
    "OIDC_CLIENT_SECRET": "KEYCLOAK_CLIENT_SECRET",
    "OIDC_CALLBACK_URL": "KEYCLOAK_CALLBACK_URL",
    "OIDC_ADMIN_ROLE": "KEYCLOAK_ADMIN_ROLE",
    "OIDC_TIMEOUT": "KEYCLOAK_TIMEOUT",
    "OIDC_VERIFY_AUDIENCE": "KEYCLOAK_VERIFY_AUDIENCE",
    "OIDC_AUDIENCE": "KEYCLOAK_AUDIENCE",
    "OIDC_USE_PKCE": "KEYCLOAK_USE_PKCE",
    "OIDC_VERIFY_ISSUER": "KEYCLOAK_VERIFY_ISSUER",
    "OIDC_DISCOVERY_URL": "KEYCLOAK_DISCOVERY_URL",
    "OIDC_ISSUER": "KEYCLOAK_ISSUER",
    "OIDC_ROLES_CLAIM": "KEYCLOAK_ROLES_CLAIM",
    "OIDC_SCOPES": "KEYCLOAK_SCOPES",
}


def oidc_env(canonical: str, default: str = "", environ: Mapping[str, str] | None = None) -> str:
    """Read *canonical*, honouring its retired spelling first.

    Args:
        canonical: The ``OIDC_*`` environment variable name.
        default: Value to return when neither spelling is set to something non-empty.
        environ: Environment mapping to read; defaults to ``os.environ``.

    Returns:
        The legacy value if set and non-empty, else the canonical value if set and
        non-empty, else *default*.
    """
    env = os.environ if environ is None else environ
    legacy = LEGACY_OIDC_ENV_ALIASES.get(canonical)
    if legacy:
        value = env.get(legacy, "")
        if value:
            return value
    return env.get(canonical, "") or default


def oidc_bool_env(canonical: str, default: bool, environ: Mapping[str, str] | None = None) -> bool:
    """Read a boolean ``OIDC_*`` setting, honouring its retired spelling.

    Matches the ``"true"``-comparison used throughout ``config.py`` so a value that
    round-trips through either spelling parses identically.
    """
    raw = oidc_env(canonical, "true" if default else "false", environ=environ)
    return raw.strip().lower() == "true"


def oidc_int_env(canonical: str, default: int, environ: Mapping[str, str] | None = None) -> int:
    """Read an integer ``OIDC_*`` setting, honouring its retired spelling.

    An unparseable value falls back to *default* rather than raising: a bad timeout
    must not stop the process from starting.
    """
    raw = oidc_env(canonical, "", environ=environ)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def deprecated_oidc_env_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return the retired variable names this process was actually started with.

    Used for a single startup log line. Empty means the deployment is already on the
    canonical spelling.
    """
    env = os.environ if environ is None else environ
    return sorted(legacy for legacy in LEGACY_OIDC_ENV_ALIASES.values() if env.get(legacy))
