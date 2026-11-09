"""Names of the auth cookies, shared by the cookie helpers (``api/cookies.py``) and CSRF middleware."""

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"
#: Binds an in-flight OIDC login to the browser that started it.
OIDC_STATE_COOKIE = "oidc_state_binding"
