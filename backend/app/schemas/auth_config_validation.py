"""Validation of ``PUT /admin/auth-config/{category}`` payloads.

Validates a request body against the per-category models in
``app.schemas.auth_config`` (``CATEGORY_SCHEMAS``): allow-list, type contract,
bounds, and the cross-field rules that need the currently effective values. Also
owns ``coded_default``, the schema default a malformed stored value falls back to.
"""

from typing import Any

from pydantic import ValidationError

from app.schemas.auth_config import CATEGORY_SCHEMAS

#: Mirrors ``auth_config.config_key`` (``String(100)``). A longer key used to reach
#: Postgres and come back as a ``DataError`` wrapped in a generic 500.
MAX_CONFIG_KEY_LENGTH = 100

_FIELD_DEFAULTS: dict[str, Any] = {}


def coded_default(config_key: str, fallback: Any = None) -> Any:
    """Return the schema default for *config_key*.

    Used by the read path when a stored value cannot be parsed: falling back to
    the declared default keeps a security control (issuer/audience validation,
    LDAP TLS) at its safe setting instead of at ``False``, which is what a bare
    ``value.lower() in ("true", ...)`` produces for any garbage string.

    Args:
        config_key: Configuration key.
        fallback: Returned when the key belongs to no category schema.

    Returns:
        The declared default, or *fallback* for an unknown key.
    """
    if not _FIELD_DEFAULTS:
        for model in CATEGORY_SCHEMAS.values():
            for name, field in model.model_fields.items():
                _FIELD_DEFAULTS[name] = field.get_default(call_default_factory=True)
    return _FIELD_DEFAULTS.get(config_key, fallback)


#: Keys a category's cross-field rules need to see even when the payload does not
#: carry them. The caller loads these from the current effective config and merges
#: the payload over them — checking the payload alone would let an admin reach the
#: rejected state by saving one field at a time.
CROSS_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "local": ("local_enabled", "allow_registration"),
    "pki": ("pki_enabled", "pki_verify_revocation", "pki_ca_cert_path"),
    "password_policy": ("password_min_length", "password_max_length"),
}


def _check_cross_field_rules(
    category: str, merged: dict[str, Any], incoming_keys: frozenset[str]
) -> None:
    """Reject combinations that are individually valid but jointly incoherent.

    Args:
        category: Configuration category being written.
        merged: Resulting state — current effective values with the payload applied.
        incoming_keys: Keys the request actually carried, used to word the error
            around the change the admin is making.

    Raises:
        ValueError: The resulting state is rejected.
    """
    if category == "pki":
        _check_pki_cross_field_rules(merged, incoming_keys)
        return

    if category == "password_policy":
        _check_password_length_rules(merged)
        return

    if category != "local":
        return

    allow_registration = merged.get("allow_registration", coded_default("allow_registration"))
    local_enabled = merged.get("local_enabled", coded_default("local_enabled"))

    if not allow_registration or local_enabled:
        return

    # Self-registration hardcodes auth_type='local' plus a local password
    # (api/endpoints/auth/registration.py), so with local password login off every
    # account it mints is one that can never sign in.
    if "local_enabled" in incoming_keys and "allow_registration" not in incoming_keys:
        raise ValueError(
            "Cannot disable local password login (local_enabled) while self-registration "
            "(allow_registration) is enabled: registration creates local-password accounts "
            "that could never sign in. Turn off allow_registration first."
        )
    raise ValueError(
        "allow_registration cannot be enabled while local password login (local_enabled) is "
        "disabled: self-registration creates local-password accounts that could never sign in. "
        "Enable local_enabled first, or leave self-registration off."
    )


def _check_password_length_rules(merged: dict[str, Any]) -> None:
    """A maximum length below the minimum would reject every password.

    Raises:
        ValueError: ``password_max_length`` is set (non-zero) and below ``password_min_length``.
    """
    maximum = merged.get("password_max_length", coded_default("password_max_length"))
    minimum = merged.get("password_min_length", coded_default("password_min_length"))
    if maximum and maximum < minimum:
        raise ValueError(
            f"password_max_length ({maximum}) cannot be below password_min_length ({minimum}): "
            "no password could satisfy both. Use 0 for no maximum."
        )


def _check_pki_cross_field_rules(merged: dict[str, Any], incoming_keys: frozenset[str]) -> None:
    """Refuse revocation checking with no CA bundle to build the issuer chain from.

    ``core/config.py:_validate_pki_settings`` has always raised this at startup for
    the ``.env`` spelling. It could not see the DB copy, which did not matter while
    ``pki_auth.py`` read ``settings.PKI_*`` directly and the DB keys were inert.
    Now that they are live (issue #498), the admin UI can assemble a state the
    environment refuses to boot with — so the same rule has to hold here.

    Without the CA bundle ``_load_issuer_certificate`` returns None, OCSP is
    skipped, the CRL cross-check has nothing to verify a signature against, and
    every certificate lands on the ``pki_revocation_soft_fail`` branch: silently
    admitted, or universally rejected. Both are worse than a 400 at save time.

    Raises:
        ValueError: Revocation checking is on with no CA certificate path.
    """
    if not merged.get("pki_enabled", coded_default("pki_enabled")):
        return
    if not merged.get("pki_verify_revocation", coded_default("pki_verify_revocation")):
        return
    if merged.get("pki_ca_cert_path", coded_default("pki_ca_cert_path")):
        return

    if "pki_verify_revocation" in incoming_keys:
        raise ValueError(
            "Cannot enable pki_verify_revocation without pki_ca_cert_path: revocation "
            "checking needs a CA certificate to build the issuer chain, and without one "
            "every certificate falls through to the pki_revocation_soft_fail decision. "
            "Set pki_ca_cert_path first."
        )
    raise ValueError(
        "pki_ca_cert_path cannot be cleared while pki_verify_revocation is enabled: "
        "revocation checking would have no CA certificate to verify against. Turn off "
        "pki_verify_revocation first, or supply a different CA path."
    )


def _format_validation_error(category: str, exc: ValidationError) -> str:
    """Render a pydantic error as a message safe to return to the admin UI."""
    problems = []
    for error in exc.errors():
        key = ".".join(str(part) for part in error["loc"]) or "(payload)"
        problems.append(f"{key}: {error['msg']}")
    return f"Invalid {category} configuration — " + "; ".join(problems)


def validate_category_config(
    category: str,
    config: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a partial category payload and return it type-coerced.

    Only the keys present in *config* are returned, so a save of one field never
    rewrites the rest of the tab with defaults.

    Args:
        category: Configuration category the payload is being written to.
        config: Raw key/value pairs from the request body.
        current: Currently effective values for ``CROSS_FIELD_KEYS[category]``.
            The payload is merged over these before the cross-field rules run,
            so a rejected combination cannot be assembled one save at a time.

    Returns:
        The same keys with values coerced to their declared types.

    Raises:
        ValueError: Unknown category, over-long/unknown key, unparseable value,
            a value outside the declared bounds, or a rejected combination.
            Callers at the HTTP edge turn this into a 400 — never a 500 and never
            a silent write.
    """
    model = CATEGORY_SCHEMAS.get(category)
    if model is None:
        raise ValueError(
            f"Unknown configuration category '{category}'. "
            f"Must be one of: {', '.join(CATEGORY_SCHEMAS)}"
        )

    if not isinstance(config, dict):
        raise ValueError("Configuration payload must be a JSON object")

    over_long = sorted(key for key in config if len(key) > MAX_CONFIG_KEY_LENGTH)
    if over_long:
        truncated = ", ".join(f"{key[:32]}… ({len(key)} chars)" for key in over_long)
        raise ValueError(
            f"Configuration key(s) exceed the {MAX_CONFIG_KEY_LENGTH}-character limit: {truncated}"
        )

    unknown = sorted(set(config) - set(model.model_fields))
    if unknown:
        raise ValueError(
            f"Unknown configuration key(s) for category '{category}': {', '.join(unknown)}. "
            f"Valid keys: {', '.join(model.model_fields)}"
        )

    try:
        validated = model.model_validate(config)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(category, exc)) from exc

    cleaned = {key: getattr(validated, key) for key in config}
    _check_cross_field_rules(category, {**(current or {}), **cleaned}, frozenset(config))
    return cleaned
