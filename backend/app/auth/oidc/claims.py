"""ID-token verification and claim extraction.

**Only the ID token authenticates.** OIDC Core guarantees it is a JWT audienced to
our ``client_id``; an access token is opaque on most providers (Okta's org
authorization server, Google, Entra) and RFC 9068 §6 says a client "MUST NOT inspect
the content of the access token". The access token is still used here, but only as a
bearer credential against ``userinfo`` — which is what it is for.
"""

import logging
from typing import Any
from typing import TypedDict

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from app.auth.oidc.config import DEFAULT_ROLES_CLAIM
from app.auth.oidc.config import OIDCConfig
from app.auth.oidc.discovery import fetch_jwks
from app.auth.oidc.endpoints import resolve_endpoints

logger = logging.getLogger(__name__)

#: Signing algorithms this RP accepts for an ID token. Asymmetric only, and
#: deliberately a narrow literal rather than whatever the provider advertises:
#: Discovery §3 explicitly permits ``none`` in
#: ``id_token_signing_alg_values_supported``, and any ``HS*`` token is forgeable by
#: anyone holding the client secret (OIDC Core §3.1.3.7 #8, RFC 8725 §2.1).
#: Phase 3a of ``plans/oidc-conformance-plan.md`` makes the set configurable; it must
#: still pass through :func:`safe_signing_algorithms`.
ID_TOKEN_SIGNING_ALGORITHMS: tuple[str, ...] = ("RS256",)

#: Asymmetric JWS families. Anything outside these — ``none``, ``HS*``, or a value
#: we do not recognise — can never reach ``jwt.decode(algorithms=...)``.
_ASYMMETRIC_ALG_PREFIXES = ("RS", "ES", "PS", "EdDSA")


def safe_signing_algorithms(candidates) -> list[str]:
    """Filter *candidates* down to algorithms that are safe to accept.

    The gate, not a formatting helper: ``none`` means an unsigned token is accepted,
    and ``HS*`` means the client secret doubles as a signing key, so a client that
    can read the configuration can mint an admin ID token. Both are removed here so
    no caller can widen the set by passing a provider-supplied list.

    Args:
        candidates: Iterable of algorithm names.

    Returns:
        The accepted subset, order preserved and de-duplicated.
    """
    seen: set[str] = set()
    safe: list[str] = []
    for raw in candidates or ():
        alg = str(raw).strip()
        if not alg or alg in seen:
            continue
        seen.add(alg)
        if alg.startswith(_ASYMMETRIC_ALG_PREFIXES):
            safe.append(alg)
        else:
            logger.warning("Refusing unsafe ID-token signing algorithm %r", alg)
    return safe


class OIDCUserData(TypedDict):
    """User data extracted from a verified ID token."""

    oidc_subject: str
    email: str
    email_verified: bool
    full_name: str
    username: str
    is_admin: bool
    roles: list[str]
    #: Top-level ID-token claim **names** only, never values — diagnostic surface
    #: for "the roles claim is misconfigured and nobody notices until permissions
    #: are wrong" (P1.2). An admin looking at the audit log after a first login
    #: can see whether ``groups`` exists and ``realm_access`` does not, without
    #: this becoming a second place group/claim values leak to.
    claim_keys: list[str]
    #: Where ``roles`` actually came from: the ID token itself, the userinfo
    #: fallback (:func:`_roles_from_userinfo`), or neither.
    roles_claim_source: str
    #: True when the provider signalled it withheld group membership rather
    #: than the identity genuinely having none (Entra's groups-overage claim
    #: shape). ``roles`` is ``[]`` either way; this is what makes the two
    #: cases distinguishable to a caller deciding whether an empty list means
    #: "admit with no elevated role" or "we could not find out — fail loudly."
    groups_overage: bool
    #: True when the token's issuer is a provider that structurally never
    #: emits a groups claim at all (Google Workspace) — not a per-login
    #: condition, a property of the provider, so it warrants the same loud
    #: treatment as an overage even though ``_claim_names`` never appears.
    groupless_provider: bool
    cert_dn: str | None
    cert_serial: str | None
    cert_issuer: str | None
    cert_org: str | None
    cert_ou: str | None
    cert_valid_from: str | None
    cert_valid_until: str | None
    cert_fingerprint: str | None


def _extract_certificate_claims(token_claims: dict) -> dict:
    """Extract certificate claims from an OIDC token.

    When the provider brokers X.509 certificate authentication, certificate metadata
    may be included in the token claims.
    """
    return {
        "cert_dn": token_claims.get("cert_dn") or token_claims.get("x509_cert_dn"),
        "cert_serial": token_claims.get("cert_serial") or token_claims.get("x509_cert_serial"),
        "cert_issuer": token_claims.get("cert_issuer") or token_claims.get("x509_cert_issuer"),
        "cert_org": token_claims.get("cert_org") or token_claims.get("x509_cert_org"),
        "cert_ou": token_claims.get("cert_ou") or token_claims.get("x509_cert_ou"),
        "cert_valid_from": token_claims.get("cert_valid_from")
        or token_claims.get("x509_cert_not_before"),
        "cert_valid_until": token_claims.get("cert_valid_until")
        or token_claims.get("x509_cert_not_after"),
        "cert_fingerprint": token_claims.get("cert_fingerprint")
        or token_claims.get("x509_cert_sha256_fingerprint"),
    }


def _claim_by_path(claims: dict, path: str) -> Any:
    """Read a dotted claim path (``realm_access.roles``, ``groups``, ``resource.a.b``)."""
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _normalize_roles(value: Any) -> list[str] | None:
    """Coerce a roles/groups claim to a list of names, or None when absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return None


#: Google's two valid issuer spellings (oidc-conformance-plan.md §6). Google's
#: discovery document has no groups claim in ``claims_supported`` at all — there
#: is no scope or claim name that would ever populate one, unlike a provider
#: that merely omitted a mapper.
_GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


def _is_google_issuer(issuer: str) -> bool:
    return issuer in _GOOGLE_ISSUERS


def _has_groups_overage(payload: dict, roles_claim: str) -> bool:
    """Detect Entra ID's groups-overage shape.

    Above 200 group memberships, Entra omits the ``groups`` claim entirely and
    replaces it with the *aggregated/distributed claims* markers
    ``_claim_names``/``_claim_sources`` (pointing at Graph) — plus, on some
    token versions, a bare ``hasgroups: true``. Either shape means "this
    identity's groups exist but were not included," which is a completely
    different fact from "this identity has no groups": an RP that reads
    ``groups`` naively sees zero groups for exactly its most-privileged users
    (oidc-conformance-plan.md §6).
    """
    claim_names = payload.get("_claim_names")
    if isinstance(claim_names, dict) and roles_claim.split(".")[0] in claim_names:
        return True
    return bool(payload.get("hasgroups"))


def _decode_token(token: str, jwks: dict, cfg: OIDCConfig, expected_issuer: str) -> dict | None:
    """Verify *token* against the JWKS and return its claims, or None if invalid.

    Two separate joserfc calls, deliberately: ``jwt.decode`` verifies the
    signature and the algorithm allow-list only — unlike python-jose it does
    **not** check ``exp``/``aud``/``iss`` on its own (confirmed empirically: an
    expired token decodes without error). Claim validation is a second,
    explicit step via :class:`JWTClaimsRegistry`, so ``exp`` is always
    essential regardless of the admin's audience/issuer settings — an
    expired-but-otherwise-valid token must never be accepted.
    """
    algorithms = safe_signing_algorithms(ID_TOKEN_SIGNING_ALGORITHMS)
    if not algorithms:
        logger.error("No safe ID-token signing algorithm is configured — refusing the token")
        return None

    try:
        # `jwks` is `dict[str, Any]` (fetch_jwks's return type, cache-friendly);
        # joserfc wants its own KeySetSerialization TypedDict shape. Runtime
        # validation of the actual RFC 7517 structure happens inside
        # import_key_set itself, which is what the except below is for.
        keyset = KeySet.import_key_set(jwks)  # type: ignore[arg-type]
    except JoseError as e:
        logger.warning(f"Malformed JWKS (JoseError): {e}")
        return None

    try:
        token_obj = jwt.decode(token, keyset, algorithms=algorithms)
    except JoseError as e:
        logger.warning(f"Invalid OIDC token (JoseError): {e}")
        return None

    claims_options: dict[str, Any] = {"exp": {"essential": True}}
    if cfg.verify_audience:
        audience = cfg.audience or cfg.client_id
        claims_options["aud"] = {"essential": True, "value": audience}
        logger.debug(f"Validating token audience against: {audience}")
    if cfg.verify_issuer:
        claims_options["iss"] = {"essential": True, "value": expected_issuer}
        logger.debug(f"Validating token issuer against: {expected_issuer}")

    try:
        JWTClaimsRegistry(**claims_options).validate(token_obj.claims)
    except JoseError as e:
        logger.warning(f"Invalid OIDC token claims (JoseError): {e}")
        return None

    payload: dict = token_obj.claims
    return payload


async def _roles_from_userinfo(access_token: str, cfg: OIDCConfig, endpoints: dict) -> list[str]:
    """Read the configured roles claim from the userinfo endpoint.

    Several providers keep group membership out of the ID token unless a dedicated
    scope was granted, and userinfo is the OIDC-defined place to look it up. The
    access token is used here as a **bearer credential**, never parsed. Failure is
    non-fatal: the user simply logs in without elevated roles.
    """
    url = endpoints.get("userinfo")
    if not url or not access_token:
        return []

    try:
        async with httpx.AsyncClient(timeout=float(cfg.timeout)) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            response.raise_for_status()
            claims = response.json()
    except httpx.HTTPError as e:
        logger.warning(f"Failed to fetch userinfo for role mapping: {e}")
        return []
    except ValueError as e:
        logger.warning(f"Malformed userinfo response: {e}")
        return []

    if not isinstance(claims, dict):
        return []
    return _normalize_roles(_claim_by_path(claims, cfg.roles_claim or DEFAULT_ROLES_CLAIM)) or []


async def validate_token(
    access_token: str,
    cfg: OIDCConfig | None = None,
    id_token: str | None = None,
) -> OIDCUserData | None:
    """Validate the ID token and extract user data.

    There is **no fallback to the access token**. The previous implementation tried
    the ID token and then the access token, accepting the first that verified — so an
    ID token that failed audience or issuer validation silently fell through to a
    credential the RP is not entitled to inspect (RFC 9068 §6), whose ``aud`` means
    something different, and which on Okta/Google/Entra is not a JWT at all. That is
    an attacker-influenceable downgrade, not a fallback. A missing or invalid ID token
    is now a hard failure.

    Args:
        access_token: Access token from the exchange — used only against ``userinfo``.
        cfg: Resolved OIDC configuration (if None, loads from env).
        id_token: ID token from the same exchange. Required.
    """
    if cfg is None:
        cfg = OIDCConfig.from_env()

    if not id_token:
        logger.error(
            "OIDC token response carried no id_token — refusing the login. The provider "
            "must be granted the 'openid' scope for this client."
        )
        return None

    try:
        endpoints = await resolve_endpoints(cfg, internal=True)
        jwks = await fetch_jwks(endpoints.get("certs", ""), timeout=float(cfg.timeout))
        if not jwks:
            logger.error("Failed to fetch JWKS for token validation")
            return None

        logger.debug(f"JWKS fetched successfully, keys count: {len(jwks.get('keys', []))}")

        expected_issuer = endpoints.get("issuer") or f"{cfg.server_url}/realms/{cfg.realm}"

        payload = _decode_token(id_token, jwks, cfg, expected_issuer)
        if payload is None:
            return None

        logger.info(
            f"ID token decoded successfully for user: "
            f"{payload.get('preferred_username', 'unknown')}"
        )

        roles_claim = cfg.roles_claim or DEFAULT_ROLES_CLAIM
        roles_claim_source = "id_token"
        roles = _normalize_roles(_claim_by_path(payload, roles_claim))
        if roles is None:
            roles_claim_source = "userinfo"
            roles = await _roles_from_userinfo(access_token, cfg, endpoints)
            if not roles:
                roles_claim_source = "absent"
        is_admin = cfg.admin_role in roles

        groups_overage = _has_groups_overage(payload, roles_claim)
        groupless_provider = _is_google_issuer(expected_issuer)
        if (groups_overage or groupless_provider) and not roles:
            # Loud, not a silent empty list: cfg.admin_role may be sitting
            # unmatched in a claim this login never actually saw, which would
            # otherwise silently demote an admin on every login rather than
            # only when their group membership genuinely changed.
            logger.error(
                "OIDC login for subject %s: the '%s' claim was withheld by the "
                "provider (%s), not genuinely absent — roles/groups could not be "
                "determined for this login. Group-based admission and "
                "admin_role grants are unreliable until this is addressed "
                "(Entra: request the claim via Graph, or reduce group count "
                "below the overage threshold; Google: this provider has no "
                "groups claim at all, so group-based admission/role config "
                "must not be relied on here).",
                payload.get("sub", "unknown"),
                roles_claim,
                "groups overage" if groups_overage else "no groups claim on this provider",
            )

        cert_claims = _extract_certificate_claims(payload)

        # For government deployments where the IdP acts as the X.509/PKI broker,
        # also honour PKI_ADMIN_DNS — a cert DN in that list grants admin regardless
        # of whether the provider role is assigned.
        if not is_admin and cert_claims.get("cert_dn"):
            from app.auth.pki_auth import _is_pki_admin

            if _is_pki_admin(cert_claims["cert_dn"]):
                is_admin = True
                logger.info(
                    "OIDC user promoted to admin via PKI cert DN: %s",
                    cert_claims["cert_dn"],
                )

        return OIDCUserData(
            oidc_subject=payload["sub"],
            email=payload.get("email", ""),
            # Absent means unverified. The claim is the provider's assertion that it
            # owns the address; treating "not stated" as "verified" is what makes
            # email-match account linking a takeover vector (auth/account_linking.py).
            email_verified=bool(payload.get("email_verified", False)),
            full_name=payload.get("name", ""),
            username=payload.get("preferred_username", ""),
            is_admin=is_admin,
            roles=roles,
            claim_keys=sorted(str(k) for k in payload),
            roles_claim_source=roles_claim_source,
            groups_overage=groups_overage,
            groupless_provider=groupless_provider,
            cert_dn=cert_claims["cert_dn"],
            cert_serial=cert_claims["cert_serial"],
            cert_issuer=cert_claims["cert_issuer"],
            cert_org=cert_claims["cert_org"],
            cert_ou=cert_claims["cert_ou"],
            cert_valid_from=cert_claims["cert_valid_from"],
            cert_valid_until=cert_claims["cert_valid_until"],
            cert_fingerprint=cert_claims["cert_fingerprint"],
        )
    except Exception as e:
        # JWT signature/claim failures are handled in _decode_token; this catches the
        # rest (network, malformed JWKS) so a login attempt can never 500.
        logger.error(f"Error validating OIDC token: {type(e).__name__}: {e}")
        return None
