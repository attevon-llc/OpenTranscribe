"""The python3-saml settings/request bridge.

Every function here delegates signature verification, XML parsing and assertion
decryption to ``onelogin.saml2`` (python3-saml). Nothing in this module parses SAML
XML itself — see the package docstring for why that boundary is load-bearing.
"""

import logging

from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.settings import OneLogin_Saml2_Settings

from app.auth.saml.config import SAMLConfig

logger = logging.getLogger(__name__)


def build_settings(cfg: SAMLConfig) -> dict:
    """Translate a resolved :class:`SAMLConfig` into python3-saml's settings dict.

    ``strict=True`` is not optional — it is what makes python3-saml enforce
    Destination/Recipient/audience/timing checks on every assertion. python3-saml
    documents ``strict=False`` as a debugging aid only.
    """
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": cfg.sp_entity_id,
            "assertionConsumerService": {
                "url": cfg.sp_acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url": cfg.sp_sls_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
            "x509cert": cfg.sp_x509_cert,
            "privateKey": cfg.sp_private_key,
        },
        "idp": {
            "entityId": cfg.idp_entity_id,
            "singleSignOnService": {
                "url": cfg.idp_sso_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "singleLogoutService": {
                "url": cfg.idp_slo_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": cfg.idp_x509_cert,
        },
        "security": {
            # The IdP must sign what it asserts (OWASP-recommended posture) —
            # enforced by python3-saml against idp.x509cert, never by this module.
            "wantAssertionsSigned": cfg.want_assertions_signed,
            "wantMessagesSigned": cfg.want_messages_signed,
            "authnRequestsSigned": cfg.sign_authn_requests,
            "wantNameId": True,
            "wantAttributeStatement": True,
            "logoutRequestSigned": cfg.sign_authn_requests,
            "logoutResponseSigned": cfg.sign_authn_requests,
        },
    }


def build_auth(request_data: dict, cfg: SAMLConfig) -> OneLogin_Saml2_Auth:
    """Construct the python3-saml ``Auth`` object for one request."""
    settings = OneLogin_Saml2_Settings(settings=build_settings(cfg), sp_validation_only=False)
    return OneLogin_Saml2_Auth(request_data, old_settings=settings)
