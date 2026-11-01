"""A blank-but-non-empty ``admin_groups`` value must grant admin to nobody."""

from __future__ import annotations

import pytest

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _is_ldap_admin

STAFF = "CN=Staff,DC=test"
ADMINS = "CN=Admins,DC=test"


@pytest.mark.parametrize("blank", [";", " ", " ; ", ";;", "\t;\n"])
def test_blank_admin_groups_grant_admin_to_nobody(blank: str) -> None:
    cfg = LdapConfig(admin_groups=blank)

    assert _is_ldap_admin(cfg, "alice", [STAFF]) is False


def test_blank_admin_groups_do_not_hide_a_real_admin_users_entry() -> None:
    cfg = LdapConfig(admin_groups=";", admin_users="alice")

    assert _is_ldap_admin(cfg, "alice", [STAFF]) is True
    assert _is_ldap_admin(cfg, "bob", [STAFF]) is False


def test_real_admin_group_still_grants_admin_only_to_members() -> None:
    cfg = LdapConfig(admin_groups=f" ; {ADMINS} ;")

    assert _is_ldap_admin(cfg, "alice", [STAFF, ADMINS]) is True
    assert _is_ldap_admin(cfg, "bob", [STAFF]) is False
