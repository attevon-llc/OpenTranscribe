"""SAML 2.0 service-provider support (#35).

Split the same way ``app/auth/oidc/`` is: ``config.py`` (DB > .env > default),
``sp.py`` (the python3-saml settings/request bridge — signature verification is
python3-saml's, never hand-rolled), ``admission.py`` (allow/block group gate,
mirroring ``oidc/admission.py``), ``provisioning.py`` (JIT).

**Never implement assertion parsing or signature validation here.** XML signature
wrapping has produced critical auth bypasses in Shibboleth and numerous commercial
SPs — that is exactly the class of bug this module exists to avoid by delegating to
python3-saml (``onelogin.saml2``), a maintained toolkit with an OWASP-reviewed XML
signature implementation, instead of parsing SAML XML by hand.
"""

from app.auth.saml.config import SAMLConfig

__all__ = ["SAMLConfig"]
