"""Fail-closed environment gating and bootstrap-admin seeding (#284 A0.3/A0.4/A0.9).

Before this, `ENVIRONMENT` defaulted to "development" AND nothing ever passed it into
the containers — `opentr.sh` uses a shell-local variable of the same name and exports
`BUILD_ENV` instead. So `settings.ENVIRONMENT` was always "development" in every
deployment, including `./opentr.sh start prod`, and every check gated on
`ENVIRONMENT in ("production", "prod")` was dead code: default-secret refusal, DEBUG
enforcement, the Redis-password requirement, and the cookie Secure flag.

The gate is now inverted — hardened unless explicitly told otherwise.
"""

from __future__ import annotations

import pytest

from app.core.config import RELAXED_ENVIRONMENTS
from app.core.config import is_relaxed_environment
from tests.helpers import does_not_raise

# ── The gate itself ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", sorted(RELAXED_ENVIRONMENTS))
def test_declared_relaxed_environments_are_relaxed(value):
    assert is_relaxed_environment(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",  # unset / empty
        "production",
        "prod",
        "staging",
        "stage",
        "qa",
        "developement",  # typo — must NOT relax
        "dev-1",
        "production ",
        "not-development",
    ],
)
def test_everything_else_is_hardened(value):
    assert is_relaxed_environment(value) is False


@pytest.mark.parametrize(
    "value", ["DEVELOPMENT", "Development", "DeV", " development ", "\ttesting\n"]
)
def test_gate_is_case_and_whitespace_tolerant(value):
    """An operator writing ENVIRONMENT=Development still gets a dev stack."""
    assert is_relaxed_environment(value) is True


def test_relaxed_set_is_closed():
    """Guard against someone quietly widening the relaxed set."""
    assert {"development", "dev", "testing", "test", "local"} == RELAXED_ENVIRONMENTS


# ── Settings wiring ──────────────────────────────────────────────────────────────


def test_default_environment_is_hardened(run_in_clean_process, tmp_path):
    """The whole point of A0.3: an unset ENVIRONMENT must NOT relax anything.

    Runs in a clean child process with ENVIRONMENT removed. It cannot be done
    in-process: `ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")` binds its
    default when the class body executes, and the root conftest sets
    ENVIRONMENT=testing before app.* is imported.
    """
    out = run_in_clean_process(
        "from app.core.config import settings;"
        "print(f'{settings.ENVIRONMENT}|{settings.is_hardened}|{settings.DEBUG}')",
        unset=("ENVIRONMENT",),
        UPLOAD_DIR=str(tmp_path / "up"),
        TEMP_DIR=str(tmp_path / "tmp"),
    )
    environment, hardened, debug = out.split("|")

    assert environment == "production", "unset ENVIRONMENT must default to production"
    assert hardened == "True", "unset ENVIRONMENT must fail closed"
    assert debug == "False", "DEBUG must be off when ENVIRONMENT is unset"


@pytest.mark.parametrize(
    ("environment", "hardened"),
    [
        ("development", False),
        ("testing", False),
        ("production", True),
        ("staging", True),
        ("", True),
    ],
)
def test_is_hardened_tracks_environment(environment, hardened):
    from app.core.config import Settings

    settings = Settings(_env_file=None, ENVIRONMENT=environment)
    assert settings.is_hardened is hardened


# ── Bootstrap admin (A0.9) ───────────────────────────────────────────────────────


def test_dev_seeds_the_well_known_credential(monkeypatch):
    """The e2e suite and local workflow depend on this exact login."""
    from app.core.config import settings
    from app.initial_data import _resolve_bootstrap_admin

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    email, password, generated = _resolve_bootstrap_admin()

    assert email == "admin@example.com"
    assert password == "password"  # gitleaks:allow
    assert generated is False


def test_hardened_never_seeds_the_well_known_password(monkeypatch):
    """A public deploy must never ship with a known super-admin login."""
    from app.core.config import settings
    from app.initial_data import _resolve_bootstrap_admin

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "INITIAL_ADMIN_PASSWORD", None)

    _, password, generated = _resolve_bootstrap_admin()

    assert password != "password"  # gitleaks:allow
    assert generated is True
    assert len(password) >= 24, "generated password should carry real entropy"


def test_hardened_generates_a_distinct_password_each_time(monkeypatch):
    from app.core.config import settings
    from app.initial_data import _resolve_bootstrap_admin

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "INITIAL_ADMIN_PASSWORD", None)

    first = _resolve_bootstrap_admin()[1]
    second = _resolve_bootstrap_admin()[1]
    assert first != second


def test_hardened_honors_explicit_initial_admin(monkeypatch):
    from app.core.config import settings
    from app.initial_data import _resolve_bootstrap_admin

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "INITIAL_ADMIN_EMAIL", "ops@example.org")
    monkeypatch.setattr(settings, "INITIAL_ADMIN_PASSWORD", "a-real-operator-password")

    email, password, generated = _resolve_bootstrap_admin()

    assert (email, password, generated) == ("ops@example.org", "a-real-operator-password", False)


# ── Boot refusal (A0.4) ──────────────────────────────────────────────────────────


def test_hardened_refuses_default_jwt_secret(monkeypatch):
    from app.core.config import settings
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "this_should_be_changed_in_production")

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _validate_production_secrets()


def test_hardened_refuses_placeholder_jwt_secret(monkeypatch):
    """Catches a hand-copied .env.example rather than a generated one."""
    from app.core.config import settings
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "CHANGE_ME_auto_generated_on_install")

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _validate_production_secrets()


def test_unset_environment_still_refuses_default_secret(monkeypatch):
    """The exact regression: no ENVIRONMENT set must NOT mean 'skip the checks'."""
    from app.core.config import settings
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "this_should_be_changed_in_production")

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _validate_production_secrets()


def test_development_boots_with_defaults(monkeypatch):
    """Self-host and the test suite must still start without configuring anything."""
    from app.core.config import settings
    from app.main import _validate_production_secrets

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "this_should_be_changed_in_production")

    with does_not_raise("development tolerates the shipped defaults, so boot validation must pass"):
        _validate_production_secrets()  # must not raise
