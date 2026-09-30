"""``GET /auth/methods`` publishes the session timeouts the SPA's idle guard uses (#1106).

A deployment whose sign-in is delegated to an external identity provider has no
server-side session row to time out, so the browser enforces the idle limit and
needs the configured values. They come from the same layered configuration
(admin UI > ``.env`` > default) the built-in session enforcement reads, so the two
can never disagree.
"""

from app.core import auth_settings as auth_settings_module


class TestSessionTimeoutsArePublished:
    def test_both_values_are_present_and_integers(self, client):
        body = client.get("/api/auth/methods").json()

        assert isinstance(body["session_idle_timeout_minutes"], int)
        assert isinstance(body["session_absolute_timeout_minutes"], int)

    def test_values_follow_the_layered_auth_config(self, client, monkeypatch):
        monkeypatch.setattr(
            auth_settings_module.DynamicAuthSettings,
            "session_idle_timeout_minutes",
            property(lambda self: 27),
        )
        monkeypatch.setattr(
            auth_settings_module.DynamicAuthSettings,
            "session_absolute_timeout_minutes",
            property(lambda self: 611),
        )

        body = client.get("/api/auth/methods").json()

        assert body["session_idle_timeout_minutes"] == 27
        assert body["session_absolute_timeout_minutes"] == 611
