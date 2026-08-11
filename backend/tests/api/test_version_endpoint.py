"""GET /api/version — the running build's identity.

Three properties this pins, each of which something downstream depends on:

* **Unauthenticated.** The release harness asserts the version BEFORE it has an
  account (fresh install), a load balancer has no credentials, and a user filing
  a bug should be able to read it. ``/health`` already returns the version, so
  requiring auth here would protect nothing.
* **No database.** "What version is running?" is asked most urgently when the
  stack is unhealthy. Schema state belongs on ``/health/ready``, which already
  owns dependency probing.
* **A stable shape.** The upgrade scenario compares ``version`` against the
  release under test; that assertion is what makes the test prove the new code is
  running rather than merely that a container started.
"""

from __future__ import annotations

from app.core.version import APP_VERSION


def test_version_is_public(client):
    """No Authorization header — must still answer."""
    response = client.get("/api/version")
    assert response.status_code == 200, response.text


def test_version_payload_shape(client):
    payload = client.get("/api/version").json()

    assert set(payload) == {"version", "git_sha", "build_time", "api_version"}
    for key, value in payload.items():
        assert isinstance(value, str) and value, f"{key} must be a non-empty string"


def test_version_matches_the_running_build(client):
    payload = client.get("/api/version").json()
    assert payload["version"] == APP_VERSION


def test_version_agrees_with_health(client):
    """Two endpoints, one fact. They must not be able to disagree."""
    version_payload = client.get("/api/version").json()
    health_payload = client.get("/health").json()
    assert version_payload["version"] == health_payload["version"]


def test_unknown_is_the_only_permitted_placeholder(client):
    """Fields fall back to the literal "unknown", never to empty or None.

    The release harness and AboutModal both branch on this exact sentinel: a
    released image reporting "unknown" is a build that forgot its --build-arg.
    """
    payload = client.get("/api/version").json()
    for key in ("version", "git_sha", "build_time"):
        value = payload[key]
        assert value == "unknown" or value.strip() == value
        assert value != "None"
