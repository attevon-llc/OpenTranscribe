"""Extracting application-shaped user data from a verified SAML assertion.

By the time anything here runs, ``onelogin.saml2.auth.OneLogin_Saml2_Auth`` has
already verified the assertion's signature (and the response's, if the IdP also
signs the envelope) — this module only reads attributes off an object that
python3-saml has already validated. It parses no XML and checks no signature.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from onelogin.saml2.auth import OneLogin_Saml2_Auth

from app.auth.saml.config import SAMLConfig

logger = logging.getLogger(__name__)

#: SAML has no standard "this address is verified" assertion the way OIDC's
#: `email_verified` claim does — the NameID/email is simply whatever the IdP put in
#: the assertion. Hardcoded False, matching PKI's and LDAP's posture, so the
#: account-takeover guard in `account_linking.assert_email_link_permitted` stays
#: closed for every SAML deployment rather than being an admin-togglable setting
#: someone could accidentally open.
SAML_ASSERTS_EMAIL_VERIFIED = False


class SAMLUserData(TypedDict):
    """User data extracted from a verified SAML assertion."""

    saml_subject: str
    email: str
    email_verified: bool
    full_name: str
    groups: list[str]
    is_admin: bool


def _first_attribute(auth: OneLogin_Saml2_Auth, name: str) -> str:
    """Return the first value of a (possibly multi-valued) SAML attribute, or ''."""
    if not name:
        return ""
    values = auth.get_attribute(name)
    return str(values[0]) if values else ""


def extract_saml_user_data(auth: OneLogin_Saml2_Auth, cfg: SAMLConfig) -> SAMLUserData:
    """Read the fields this app needs off an authenticated python3-saml ``Auth``.

    Args:
        auth: A python3-saml ``Auth`` instance after a successful
            ``process_response()`` — caller must check ``is_authenticated()`` and
            ``get_errors()`` first.
        cfg: Resolved :class:`SAMLConfig`, for the configured attribute names.

    Returns:
        The verified assertion's data in the shape the rest of the app expects.
    """
    subject = auth.get_nameid() or ""
    email = _first_attribute(auth, cfg.email_attribute) or subject
    full_name = _first_attribute(auth, cfg.name_attribute)
    groups = [str(g) for g in (auth.get_attribute(cfg.groups_attribute) or [])]

    is_admin = bool(cfg.admin_group) and any(
        g.strip().lower() == cfg.admin_group.strip().lower() for g in groups
    )

    return SAMLUserData(
        saml_subject=subject,
        email=email,
        email_verified=SAML_ASSERTS_EMAIL_VERIFIED,
        full_name=full_name,
        groups=groups,
        is_admin=is_admin,
    )
