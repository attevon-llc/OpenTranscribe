"""One quarantine read rule: exempt only where the platform role reached the row.

``PlatformBypass.user_is_admin`` is the bare role; ``allows()`` is whether that role may
act on THIS row (single mode: any admin; multi mode: only under a grant for the row's
tenant). Every quarantine read decision goes through ``takedown_predicates.quarantine_exempt``
/ ``is_quarantine_hidden`` / ``quarantine_list_exempt``; nothing derives the exemption from
the bare role.
"""

from __future__ import annotations

import ast
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.platform_access import TenancyMode
from app.services.platform_bypass import PlatformBypass
from app.services.platform_bypass import SupportGrantView
from app.services.takedown_predicates import is_quarantine_hidden
from app.services.takedown_predicates import quarantine_exempt
from app.services.takedown_predicates import quarantine_list_exempt

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _file(*, quarantined=True, org=7, owner=1) -> MediaFile:
    return MediaFile(
        is_quarantined=quarantined,
        organization_id=org,
        user_id=owner,
        uuid="f-1",
        storage_path="media/x.wav",
        thumbnail_path="media/x.jpg",
    )


def _admin() -> User:
    return User(id=99, role="admin")


def _grant(org_id):
    return SupportGrantView(
        id=1,
        uuid="g-1",
        organization_id=org_id,
        subject_user_id=None,
        access_level="read",
        grant_mode="approved",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.fixture(autouse=True)
def _no_use_rows(monkeypatch):
    monkeypatch.setattr("app.services.support_access_service.record_use", lambda *a, **k: None)


class TestRuleTable:
    def test_single_mode_admin_is_exempt(self):
        bypass = PlatformBypass(user=_admin(), mode=TenancyMode.SINGLE)
        assert quarantine_exempt(bypass, _file()) is True
        assert is_quarantine_hidden(bypass, _file()) is False

    def test_multi_mode_admin_without_a_grant_is_not_exempt(self):
        bypass = PlatformBypass(user=_admin(), mode=TenancyMode.MULTI)
        assert bypass.user_is_admin is True
        assert quarantine_exempt(bypass, _file()) is False
        assert is_quarantine_hidden(bypass, _file()) is True

    def test_multi_mode_grant_exempts_only_its_own_tenant(self):
        bypass = PlatformBypass(user=_admin(), mode=TenancyMode.MULTI, grant=_grant(7))
        assert is_quarantine_hidden(bypass, _file(org=7)) is False
        assert is_quarantine_hidden(bypass, _file(org=8)) is True

    def test_a_non_admin_is_never_exempt(self):
        user = User(id=5, role="user")
        bypass = PlatformBypass(user=user, mode=TenancyMode.SINGLE)
        assert is_quarantine_hidden(bypass, _file()) is True

    def test_a_file_that_is_not_quarantined_is_never_hidden(self):
        bypass = PlatformBypass.none()
        assert is_quarantine_hidden(bypass, _file(quarantined=False)) is False

    def test_list_form_follows_the_bypass_list_reach(self):
        single = PlatformBypass(user=_admin(), mode=TenancyMode.SINGLE)
        multi_bare = PlatformBypass(user=_admin(), mode=TenancyMode.MULTI)
        multi_org = PlatformBypass(user=_admin(), mode=TenancyMode.MULTI, grant=_grant(7))
        assert quarantine_list_exempt(single) is True
        assert quarantine_list_exempt(multi_bare) is False
        assert quarantine_list_exempt(multi_org) is True


class TestSetFileUrls:
    """``set_file_urls`` is the one place a thumbnail/download URL is stamped on a file."""

    def _stamp(self, bypass, monkeypatch):
        from app.api.endpoints.files.crud import set_file_urls

        monkeypatch.setenv("SKIP_S3", "true")
        row = _file()
        set_file_urls(row, bypass)
        return row

    def test_multi_mode_admin_without_a_grant_gets_no_urls(self, monkeypatch):
        row = self._stamp(PlatformBypass(user=_admin(), mode=TenancyMode.MULTI), monkeypatch)
        assert (row.download_url, row.thumbnail_url) == (None, None)

    def test_single_mode_admin_still_gets_urls(self, monkeypatch):
        row = self._stamp(PlatformBypass(user=_admin(), mode=TenancyMode.SINGLE), monkeypatch)
        assert row.download_url == "/api/files/f-1/prepare-download"
        assert row.thumbnail_url == "/api/files/f-1/thumbnail"


_GUARDED = {"is_hidden_for": "is_admin", "exclude_quarantined": "include_quarantined"}


def _mentions_user_is_admin(node: ast.AST, tainted: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "user_is_admin":
            return True
        if isinstance(sub, ast.Name) and sub.id in tainted:
            return True
    return False


def find_role_derived_quarantine_args(source: str) -> list[tuple[int, str]]:
    """Calls to a quarantine predicate whose exemption argument comes from ``user_is_admin``."""
    tree = ast.parse(source)
    findings: list[tuple[int, str]] = []
    scopes: list[ast.AST] = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    scopes.append(tree)
    for scope in scopes:
        tainted: set[str] = set()
        for stmt in ast.walk(scope):
            if isinstance(stmt, ast.Assign) and _mentions_user_is_admin(stmt.value, set()):
                tainted |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
        for call in ast.walk(scope):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            kw_name = _GUARDED.get(name or "")
            if kw_name is None:
                continue
            for kw in call.keywords:
                if kw.arg == kw_name and _mentions_user_is_admin(kw.value, tainted):
                    findings.append((call.lineno, name or ""))
    return sorted(set(findings))


def test_no_quarantine_predicate_takes_its_exemption_from_the_bare_role():
    offenders = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        for line, name in find_role_derived_quarantine_args(path.read_text()):
            offenders.append(f"{path.relative_to(APP_ROOT)}:{line} {name}")
    assert offenders == [], (
        "derive the exemption with takedown_predicates.quarantine_exempt / "
        f"is_quarantine_hidden / quarantine_list_exempt, not user_is_admin: {offenders}"
    )


class TestGuardDetector:
    def test_fires_on_a_direct_attribute(self):
        src = "def f(ctx, m):\n    return is_hidden_for(m, is_admin=ctx.bypass.user_is_admin)\n"
        assert find_role_derived_quarantine_args(src) == [(2, "is_hidden_for")]

    def test_fires_through_a_local_variable(self):
        src = (
            "def f(ctx, q):\n"
            "    allowed = ctx.bypass.user_is_admin\n"
            "    return exclude_quarantined(q, include_quarantined=allowed)\n"
        )
        assert find_role_derived_quarantine_args(src) == [(3, "exclude_quarantined")]

    def test_stays_quiet_for_the_shared_helper_and_literals(self):
        src = (
            "def f(ctx, q, m):\n"
            "    a = is_hidden_for(m, is_admin=False)\n"
            "    return exclude_quarantined(q, include_quarantined=quarantine_list_exempt(ctx.bypass))\n"
        )
        assert find_role_derived_quarantine_args(src) == []
