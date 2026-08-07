"""Authentication API surface — endpoints **and** the dependency-injection module.

Split out of a single 2817-line module (issue #284, A3.5). There is deliberately
no ``deps.py`` in this codebase: ``get_current_user``,
``get_current_active_user``, ``get_current_admin_user``,
``get_current_active_superuser`` and ``get_optional_current_user`` are imported
from ``app.api.endpoints.auth`` by ~30 endpoint modules, so every name the flat
module exported is re-exported here.

Layout:

- :mod:`dependencies` — the DI functions and ``oauth2_scheme``. Route-free, so
  the ~30 importers never pull a router into their import graph.
- :mod:`authenticators` — per-``auth_type`` credential verification.
- :mod:`login` — ``/token`` + ``/login`` and the authentication orchestration.
- :mod:`registration` — ``/register``, ``/password-policy``, ``/password-reset/*``.
- :mod:`profile` — ``/me``, ``/session``, ``/me/certificate``.
- :mod:`keycloak` · :mod:`pki` — external IdP flows.
- :mod:`methods` — ``/methods``, ``/banner``.
- :mod:`mfa_tokens` — MFA half-token minting / scope + single-use replay protection.
- :mod:`mfa_enrollment` — the enrolment dependency and post-second-factor session
  issuance. Layered above ``mfa_tokens``; imports only ever run in that direction.
- :mod:`mfa` — the MFA endpoints.
- :mod:`sessions` — refresh rotation, logout, active sessions.
- :mod:`flower` — the nginx ``auth_request`` gate for the Flower dashboard.

Sub-routers are included in the same order the routes were declared in the flat
module. No auth route carries a path parameter, so include order is presentation
only — it cannot change which handler a request matches.
"""

from fastapi import APIRouter

from app.api.endpoints.auth.authenticators import _authenticate_ldap_user
from app.api.endpoints.auth.authenticators import _authenticate_local_user
from app.api.endpoints.auth.authenticators import _authenticate_production_user
from app.api.endpoints.auth.authenticators import _authenticate_testing_user
from app.api.endpoints.auth.authenticators import _build_user_data
from app.api.endpoints.auth.authenticators import _check_user_active
from app.api.endpoints.auth.authenticators import _ensure_user_uuid
from app.api.endpoints.auth.authenticators import _get_user_role
from app.api.endpoints.auth.dependencies import _authenticate_external_token
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_admin_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.api.endpoints.auth.dependencies import oauth2_scheme
from app.api.endpoints.auth.flower import flower_authz
from app.api.endpoints.auth.keycloak import keycloak_callback
from app.api.endpoints.auth.keycloak import keycloak_login
from app.api.endpoints.auth.login import _check_mfa_requirement
from app.api.endpoints.auth.login import _generate_login_tokens
from app.api.endpoints.auth.login import _handle_lockout_check
from app.api.endpoints.auth.login import _perform_authentication
from app.api.endpoints.auth.login import login_for_access_token
from app.api.endpoints.auth.methods import acknowledge_banner
from app.api.endpoints.auth.methods import get_auth_methods
from app.api.endpoints.auth.methods import get_login_banner
from app.api.endpoints.auth.mfa import disable_mfa
from app.api.endpoints.auth.mfa import get_mfa_status
from app.api.endpoints.auth.mfa import setup_mfa
from app.api.endpoints.auth.mfa import verify_mfa
from app.api.endpoints.auth.mfa import verify_mfa_setup
from app.api.endpoints.auth.mfa_enrollment import EnrollmentContext
from app.api.endpoints.auth.mfa_enrollment import _complete_mfa_verification
from app.api.endpoints.auth.mfa_enrollment import get_user_for_enrollment
from app.api.endpoints.auth.mfa_enrollment import issue_session_response
from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_ENROLL
from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_VERIFY
from app.api.endpoints.auth.mfa_tokens import MFA_TOKEN_BLACKLIST_PREFIX
from app.api.endpoints.auth.mfa_tokens import _blacklist_mfa_token
from app.api.endpoints.auth.mfa_tokens import _claim_mfa_token
from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
from app.api.endpoints.auth.mfa_tokens import _get_user_for_mfa
from app.api.endpoints.auth.mfa_tokens import _is_mfa_enabled
from app.api.endpoints.auth.mfa_tokens import _is_mfa_required
from app.api.endpoints.auth.mfa_tokens import _is_mfa_token_blacklisted
from app.api.endpoints.auth.mfa_tokens import _user_can_setup_mfa
from app.api.endpoints.auth.mfa_tokens import _verify_mfa_code
from app.api.endpoints.auth.mfa_tokens import _verify_mfa_token
from app.api.endpoints.auth.pki import pki_login
from app.api.endpoints.auth.profile import get_user_certificate_info
from app.api.endpoints.auth.profile import read_users_me
from app.api.endpoints.auth.profile import session_status
from app.api.endpoints.auth.registration import PasswordResetConfirmBody
from app.api.endpoints.auth.registration import PasswordResetRequestBody
from app.api.endpoints.auth.registration import confirm_password_reset_endpoint
from app.api.endpoints.auth.registration import get_password_policy
from app.api.endpoints.auth.registration import register
from app.api.endpoints.auth.registration import request_password_reset_endpoint
from app.api.endpoints.auth.sessions import get_active_sessions
from app.api.endpoints.auth.sessions import logout
from app.api.endpoints.auth.sessions import logout_all_sessions
from app.api.endpoints.auth.sessions import refresh_access_token

from . import flower as _flower_module
from . import keycloak as _keycloak_module
from . import login as _login_module
from . import methods as _methods_module
from . import mfa as _mfa_module
from . import pki as _pki_module
from . import profile as _profile_module
from . import registration as _registration_module
from . import sessions as _sessions_module

router = APIRouter()
router.include_router(_login_module.router)
router.include_router(_registration_module.router)
router.include_router(_profile_module.router)
router.include_router(_keycloak_module.router)
router.include_router(_pki_module.router)
router.include_router(_methods_module.router)
router.include_router(_mfa_module.router)
router.include_router(_sessions_module.router)
router.include_router(_flower_module.router)

__all__ = [
    "MFA_SCOPE_ENROLL",
    "MFA_SCOPE_VERIFY",
    "MFA_TOKEN_BLACKLIST_PREFIX",
    "EnrollmentContext",
    "PasswordResetConfirmBody",
    "PasswordResetRequestBody",
    "acknowledge_banner",
    "confirm_password_reset_endpoint",
    "disable_mfa",
    "flower_authz",
    "get_active_sessions",
    "get_auth_methods",
    "get_current_active_superuser",
    "get_current_active_user",
    "get_current_admin_user",
    "get_current_user",
    "get_login_banner",
    "get_mfa_status",
    "get_optional_current_user",
    "get_password_policy",
    "get_user_certificate_info",
    "get_user_for_enrollment",
    "issue_session_response",
    "keycloak_callback",
    "keycloak_login",
    "login_for_access_token",
    "logout",
    "logout_all_sessions",
    "oauth2_scheme",
    "pki_login",
    "read_users_me",
    "refresh_access_token",
    "register",
    "request_password_reset_endpoint",
    "router",
    "session_status",
    "setup_mfa",
    "verify_mfa",
    "verify_mfa_setup",
]
