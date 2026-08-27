"""Regression coverage for values resolved from a real ``.env`` FILE, not process env.

Every other config test in this tree drives resolution via ``monkeypatch.setenv`` /
``run_in_clean_process``'s child-process environment -- i.e. via process env, never a
real ``.env`` file parsed by pydantic-settings' ``dotenv`` source. That blind spot is
exactly why the ``MFA_REQUIRE_REDIS``/``PKI_REVOCATION_SOFT_FAIL``/``S3_REGION``/
``BEDROCK_REGION``/``LDAP_USER_SEARCH_FILTER`` bugs shipped past a green suite: Docker
Compose's ``env_file:`` directive injects a `.env` file's contents into the container's
process environment before the app ever starts, which happens to mask any bug that only
manifests when a value is sourced from the file *itself* rather than from
``os.environ`` -- e.g. a class-body ``os.getenv(...)`` expression that ran once at
import time, before pydantic-settings' dotenv source had a chance to load the file.

``Settings.model_config`` sets ``env_file=None`` whenever ``TESTING`` is truthy (which the
root conftest always sets), so every test here must override that explicitly via the
constructor: ``Settings(_env_file=str(env_path))``. That override works regardless of
``TESTING``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings

_REQUIRED_DIRS_ENV = """
DATA_DIR={data_dir}
MODELS_DIR={models_dir}
TEMP_DIR={temp_dir}
"""


def _write_env_file(tmp_path: Path, contents: str) -> Path:
    """Write a real .env file combining the given contents with required dir vars."""
    data_dir = tmp_path / "data"
    models_dir = tmp_path / "models"
    temp_dir = tmp_path / "temp"
    env_path = tmp_path / ".env"
    env_path.write_text(
        contents
        + _REQUIRED_DIRS_ENV.format(data_dir=data_dir, models_dir=models_dir, temp_dir=temp_dir)
    )
    return env_path


_VARS_UNDER_TEST = (
    "MFA_REQUIRE_REDIS",
    "PKI_REVOCATION_SOFT_FAIL",
    "ENVIRONMENT",
    "AWS_REGION",
    "S3_REGION",
    "BEDROCK_REGION",
    "AWS_DEFAULT_REGION",
    "LDAP_USERNAME_ATTR",
    "LDAP_USER_SEARCH_FILTER",
)


def _clear_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure process env can't mask the .env FILE's values for the vars under test."""
    for var in _VARS_UNDER_TEST:
        monkeypatch.delenv(var, raising=False)


def test_mfa_require_redis_false_from_env_file(tmp_path, monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    env_path = _write_env_file(tmp_path, "MFA_REQUIRE_REDIS=false\nENVIRONMENT=production\n")

    settings = Settings(_env_file=str(env_path))

    assert settings.MFA_REQUIRE_REDIS is False


def test_pki_revocation_soft_fail_true_from_env_file(tmp_path, monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    env_path = _write_env_file(tmp_path, "PKI_REVOCATION_SOFT_FAIL=true\nENVIRONMENT=production\n")

    settings = Settings(_env_file=str(env_path))

    assert settings.PKI_REVOCATION_SOFT_FAIL is True


def test_bool_defaults_still_apply_when_omitted_from_env_file(tmp_path, monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    env_path = _write_env_file(tmp_path, "ENVIRONMENT=production\n")

    settings = Settings(_env_file=str(env_path))

    assert settings.MFA_REQUIRE_REDIS is True
    assert settings.PKI_REVOCATION_SOFT_FAIL is False


def test_s3_and_bedrock_region_resolve_from_env_file_aws_region(tmp_path, monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    env_path = _write_env_file(tmp_path, "AWS_REGION=eu-west-2\n")

    settings = Settings(_env_file=str(env_path))

    assert settings.S3_REGION == "eu-west-2"
    assert settings.BEDROCK_REGION == "eu-west-2"


def test_ldap_search_filter_resolves_default_username_attr_from_env_file(
    tmp_path, monkeypatch
) -> None:
    _clear_process_env(monkeypatch)
    env_path = _write_env_file(tmp_path, "LDAP_USERNAME_ATTR=uid\n")

    settings = Settings(_env_file=str(env_path))

    assert settings.LDAP_USER_SEARCH_FILTER == "(uid={username})"


def test_ldap_search_filter_resolves_custom_filter_from_env_file(tmp_path, monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    env_path = _write_env_file(
        tmp_path,
        "LDAP_USER_SEARCH_FILTER=(&(objectClass=person)({username_attr}={username}))\n"
        "LDAP_USERNAME_ATTR=uid\n",
    )

    settings = Settings(_env_file=str(env_path))
    resolved = settings.LDAP_USER_SEARCH_FILTER

    assert "(uid=" in resolved
    assert "{username_attr}" not in resolved
    # The remaining placeholder must be exactly `{username}` -- a KeyError here means
    # ldap_auth.py's `.format(username=...)` call would blow up on a real bind attempt.
    resolved.format(username="alice")


def test_process_env_wins_over_env_file(tmp_path, monkeypatch) -> None:
    """Precedence guard: a var set in BOTH the file and process env -- process env wins."""
    monkeypatch.delenv("MFA_REQUIRE_REDIS", raising=False)
    env_path = _write_env_file(tmp_path, "MFA_REQUIRE_REDIS=false\nENVIRONMENT=production\n")
    monkeypatch.setenv("MFA_REQUIRE_REDIS", "true")

    settings = Settings(_env_file=str(env_path))

    assert settings.MFA_REQUIRE_REDIS is True
