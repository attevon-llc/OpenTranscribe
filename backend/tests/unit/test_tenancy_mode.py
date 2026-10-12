"""Tenancy mode detection (issue #1122, plan section 5.1).

``User.is_admin`` is an instance-wide content bypass, correct on a single-tenant install
and a cross-tenant read on a multi-tenant one. Everything downstream keys on
``tenancy_mode``, so it must fail CLOSED: anything it cannot positively call single-tenant
is multi-tenant.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.core.config import settings
from app.models.organization import Organization
from app.services import platform_access
from app.services.platform_access import TenancyMode
from app.services.platform_access import reset_tenancy_mode_cache
from app.services.platform_access import tenancy_mode
from app.services.platform_access import warn_if_forced_single_with_orgs


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    """Every test starts from ``auto`` on a community edition with a cold cache."""
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "community")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


def _add_org(db_session, *, active: bool = True) -> Organization:
    org = Organization(name=f"tenancy-{uuid.uuid4().hex[:8]}", is_active=active)
    db_session.add(org)
    db_session.flush()
    return org


def test_no_orgs_community_is_single(db_session):
    """Control: an empty organization table is the community install."""
    assert tenancy_mode(db_session) is TenancyMode.SINGLE


def test_active_org_row_makes_auto_multi(db_session):
    _add_org(db_session)
    assert tenancy_mode(db_session) is TenancyMode.MULTI


def test_inactive_org_row_does_not_make_auto_multi(db_session):
    """Only an ACTIVE organization is a tenant; a deactivated one must not flip the mode."""
    _add_org(db_session, active=False)
    assert tenancy_mode(db_session) is TenancyMode.SINGLE


def test_non_community_edition_is_multi_without_any_org(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "cloud")
    assert tenancy_mode(db_session) is TenancyMode.MULTI


def test_db_error_fails_closed_to_multi_without_latching(db_session, monkeypatch):
    """A transient outage must not strip the admin bypass for the process lifetime."""
    monkeypatch.setenv("TESTING", "false")  # the cache is bypassed under TESTING
    real_scalar = db_session.scalar
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", _flaky)

    assert tenancy_mode(db_session) is TenancyMode.MULTI
    assert tenancy_mode(db_session) is TenancyMode.SINGLE


def test_positive_multi_detection_latches_for_the_process(db_session, monkeypatch):
    """Evidence of a second tenant is cached, so a later miss cannot re-open the bypass."""
    monkeypatch.setenv("TESTING", "false")
    org = _add_org(db_session)
    assert tenancy_mode(db_session) is TenancyMode.MULTI
    db_session.delete(org)
    db_session.flush()
    assert tenancy_mode(db_session) is TenancyMode.MULTI


@pytest.mark.parametrize("value", ["", "multi-ish", "true", "SINGLE-TENANT", "0"])
def test_unknown_tenancy_mode_value_is_multi(db_session, monkeypatch, caplog, value):
    monkeypatch.setattr(settings, "TENANCY_MODE", value)
    with caplog.at_level(logging.ERROR, logger=platform_access.logger.name):
        assert tenancy_mode(db_session) is TenancyMode.MULTI
    assert "Unknown TENANCY_MODE" in caplog.text


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("single", TenancyMode.SINGLE), ("multi", TenancyMode.MULTI), (" Multi ", TenancyMode.MULTI)],
)
def test_forced_values_override_detection(db_session, monkeypatch, configured, expected):
    if expected is TenancyMode.SINGLE:
        _add_org(db_session)  # forced single wins even over an active tenant
    monkeypatch.setattr(settings, "TENANCY_MODE", configured)
    assert tenancy_mode(db_session) is expected


def test_forced_single_with_orgs_warns_at_startup(db_session, monkeypatch, caplog):
    """The escape hatch is allowed but never silent."""
    _add_org(db_session)
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    with caplog.at_level(logging.WARNING, logger=platform_access.logger.name):
        warn_if_forced_single_with_orgs(db_session)
    assert "TENANCY_MODE=single is forced" in caplog.text


def test_forced_single_without_orgs_is_quiet(db_session, monkeypatch, caplog):
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    with caplog.at_level(logging.WARNING, logger=platform_access.logger.name):
        warn_if_forced_single_with_orgs(db_session)
    assert "TENANCY_MODE=single is forced" not in caplog.text


def test_auto_mode_never_warns_at_startup(db_session, caplog):
    _add_org(db_session)
    with caplog.at_level(logging.WARNING, logger=platform_access.logger.name):
        warn_if_forced_single_with_orgs(db_session)
    assert "TENANCY_MODE=single is forced" not in caplog.text


def test_capabilities_exposes_tenancy_mode(client, db_session, monkeypatch):
    """The SPA reads the mode from the unauthenticated bootstrap route (plan A1)."""
    body = client.get("/api/system/capabilities").json()
    assert body["tenancy_mode"] == "single"

    monkeypatch.setattr(settings, "TENANCY_MODE", "multi")
    body = client.get("/api/system/capabilities").json()
    assert body["tenancy_mode"] == "multi"
