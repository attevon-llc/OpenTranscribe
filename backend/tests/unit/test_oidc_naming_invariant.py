"""The rename is complete, and this is what proves it rather than asserting it.

A user in the field asked whether OpenTranscribe's OIDC support was "hardcoded to
Keycloak". Discovery support (#353) made a generic provider *work*; it did nothing
about the fact that every field an Authentik or Okta administrator typed into was
named for a different vendor. So the whole surface was renamed ``oidc_*``.

"We renamed things" is not a guarantee — the next feature branch adds an occurrence
back and nobody notices. This test is the guarantee. It is deliberately modelled on
the existing CI seam guard that greps ``backend/app`` and ``frontend/src`` for the
managed edition's vendor nouns: a closed allow-list, each entry carrying a written
reason, and a hard failure for anything else.

Scope note: **Python source under ``backend/app/``**. Not documentation — a setup
guide that names a real product an operator has installed is correct and useful, and
the ``--with-keycloak-test`` container really is Keycloak. Not ``backend/alembic/``
either: historical revisions are immutable and describe the schema as it was. What is
guarded is the *code surface* — settings, columns, routes, DTOs — which is the thing
that told the reporter the product was single-vendor.
"""

from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: The vendor noun, assembled from parts so this test file does not match itself.
VENDOR_NOUN = "key" + "cloak"

#: Path (relative to ``backend/app``) -> why the retired spelling legitimately
#: survives there. Adding an entry is a deliberate act that has to be justified in
#: review; that is the whole mechanism.
ALLOWED: dict[str, str] = {
    "core/legacy_auth_env.py": (
        "The input adapter. Deployments own their .env file, so the historical "
        "environment-variable names keep working permanently; this module translates "
        "them onto the canonical OIDC_* spelling before Settings is built. It is an "
        "adapter over names, not a second implementation — nothing downstream, "
        "including AuthConfigService.ENV_TO_CONFIG_MAPPING, sees the old spelling."
    ),
    "db/migrations.py": (
        "Historical schema fingerprints. _detect_schema_version() probes for columns "
        "as they existed at v031 and v170 in order to stamp an untracked database. "
        "Those column names are facts about the past and cannot be renamed without "
        "breaking detection for every pre-v378 deployment."
    ),
    "schemas/user.py": (
        "AuthMethodsResponse emits a deprecated duplicate of oidc_enabled for one "
        "minor release, because a browser holding a cached SPA bundle against a "
        "freshly-upgraded backend is a real deployment state. It is a computed field "
        "so it can never disagree with oidc_enabled. Removal ticket: 'drop "
        "AuthMethodsResponse.keycloak_enabled' — delete this entry with it."
    ),
}


def _python_sources() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_vendor_noun_appears_only_where_it_is_allowed():
    """Any new occurrence under backend/app is a build failure."""
    offenders: dict[str, int] = {}
    for path in _python_sources():
        rel = path.relative_to(APP_ROOT).as_posix()
        count = path.read_text(encoding="utf-8").lower().count(VENDOR_NOUN)
        if count and rel not in ALLOWED:
            offenders[rel] = count

    assert not offenders, (
        "The OIDC surface is named oidc_* everywhere. These files reintroduce the "
        f"retired vendor spelling: {offenders}. Either rename them, or add an entry to "
        "ALLOWED in this test with a written reason — silently widening the allow-list "
        "is the failure mode this guard exists to prevent."
    )


def test_every_allowed_file_still_needs_its_exemption():
    """An allow-list entry that no longer matches anything must be deleted.

    A stale exemption is how an allow-list stops meaning anything: it accumulates
    permission for files that have since been cleaned up, and the next occurrence
    lands in one of them unchallenged.
    """
    stale = []
    for rel in ALLOWED:
        path = APP_ROOT / rel
        assert path.exists(), f"ALLOWED names {rel}, which does not exist"
        if VENDOR_NOUN not in path.read_text(encoding="utf-8").lower():
            stale.append(rel)

    assert not stale, f"These ALLOWED entries are no longer needed and must be removed: {stale}"


def test_every_exemption_carries_a_reason():
    """Mirrors the KNOWN_PUBLIC convention in test_route_privilege_tiers.py."""
    for rel, reason in ALLOWED.items():
        assert len(reason) > 60, f"{rel} needs a real reason, not {reason!r}"


def test_no_route_still_serves_the_old_path():
    """``/api/auth/keycloak/*`` is gone; the sole consumer was our own SPA.

    The IdP's registered redirect URI points at the frontend ``/login`` route, never
    at these paths, so this rename required no identity-provider reconfiguration.
    """
    from app.api.endpoints.auth import router

    paths = [route.path for route in router.routes]  # type: ignore[attr-defined]
    assert "/oidc/login" in paths
    assert "/oidc/callback" in paths
    assert not [p for p in paths if VENDOR_NOUN in p.lower()]


def test_the_auth_type_constant_is_oidc():
    from app.auth.constants import AUTH_TYPE_OIDC
    from app.auth.constants import VALID_AUTH_TYPES

    assert AUTH_TYPE_OIDC == "oidc"
    assert VENDOR_NOUN not in " ".join(VALID_AUTH_TYPES).lower()


def test_no_config_key_carries_the_old_prefix():
    """Every key the admin UI can write is oidc_*, in exactly one category."""
    from app.schemas.auth_config import CATEGORY_SCHEMAS

    assert "oidc" in CATEGORY_SCHEMAS
    assert VENDOR_NOUN not in " ".join(CATEGORY_SCHEMAS).lower()

    for category, model in CATEGORY_SCHEMAS.items():
        for key in model.model_fields:
            assert VENDOR_NOUN not in key.lower(), f"{category}.{key}"

    assert all(key.startswith("oidc_") for key in CATEGORY_SCHEMAS["oidc"].model_fields)
