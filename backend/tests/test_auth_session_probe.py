"""Tests for GET /api/auth/session — the SPA's cookie-session probe.

The probe must return 200 for EVERY caller (anonymous included): browsers
log any 401 response as a console error, and the pre-probe design
(/auth/me on page load) triggered a spurious logout cascade for anonymous
visitors that aborted the login page's own requests.
"""

from app.auth.cookies import REFRESH_COOKIE


class TestSessionProbe:
    def test_anonymous_returns_200_unauthenticated(self, client):
        response = client.get("/api/auth/session")
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is False
        assert body["refreshable"] is False
        assert body["user"] is None

    def test_bearer_token_returns_authenticated_user(self, client, user_token_headers):
        headers = {"Authorization": user_token_headers["Authorization"]}
        response = client.get("/api/auth/session", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is True
        assert body["refreshable"] is False
        assert body["user"]["email"] == user_token_headers["_test_user_email"]

    def test_garbage_token_returns_200_unauthenticated(self, client):
        """An invalid/expired token must degrade to anonymous, not 401."""
        response = client.get(
            "/api/auth/session", headers={"Authorization": "Bearer not-a-real-jwt"}
        )
        assert response.status_code == 200
        assert response.json()["authenticated"] is False

    def test_refresh_cookie_marks_session_refreshable(self, client):
        """Expired access + present refresh cookie → client should try refresh."""
        client.cookies.set(REFRESH_COOKIE, "some-refresh-token")
        try:
            response = client.get("/api/auth/session")
        finally:
            client.cookies.delete(REFRESH_COOKIE)
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is False
        assert body["refreshable"] is True

    def test_cookie_session_returns_authenticated(self, client, user_token_headers):
        """The access_token httpOnly cookie path (browser flow) works too."""
        token = user_token_headers["Authorization"].removeprefix("Bearer ")
        client.cookies.set("access_token", token)
        try:
            response = client.get("/api/auth/session")
        finally:
            client.cookies.delete("access_token")
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
