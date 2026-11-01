"""Unit tests for LDAP authentication helpers.

Covers:
- Group list parsing (GitHub issue #188: full AD DNs contain commas)
- Group membership matching
- LDAP filter injection escaping
- Email validation
- Admin resolution logic
"""

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _is_ldap_admin
from app.auth.ldap_auth import _is_valid_email
from app.auth.ldap_directory import _escape_ldap_filter
from app.auth.ldap_directory import _is_member_of_groups
from app.auth.ldap_directory import _parse_group_list


class TestParseGroupList:
    def test_empty_string(self):
        assert _parse_group_list("") == []

    def test_whitespace_only(self):
        assert _parse_group_list("   ") == []

    def test_single_full_dn(self):
        """The primary bug: a full AD DN must not be split on its internal commas."""
        result = _parse_group_list("CN=Whisper_Users,CN=Users,DC=midow,DC=local")
        assert result == ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]

    def test_multiple_full_dns_semicolon_delimited(self):
        result = _parse_group_list(
            "CN=Whisper_Users,CN=Users,DC=midow,DC=local;CN=OtherGroup,DC=midow,DC=local"
        )
        assert result == [
            "CN=Whisper_Users,CN=Users,DC=midow,DC=local",
            "CN=OtherGroup,DC=midow,DC=local",
        ]

    def test_semicolons_with_extra_whitespace(self):
        result = _parse_group_list("CN=Group1,DC=example,DC=com ; CN=Group2,DC=example,DC=com")
        assert result == [
            "CN=Group1,DC=example,DC=com",
            "CN=Group2,DC=example,DC=com",
        ]

    def test_simple_group_name_no_dn(self):
        """Simple names without '=' still work via semicolons."""
        result = _parse_group_list("Whisper_Users;Admins")
        assert result == ["Whisper_Users", "Admins"]

    def test_single_simple_name(self):
        result = _parse_group_list("Whisper_Users")
        assert result == ["Whisper_Users"]

    def test_trailing_semicolon_ignored(self):
        result = _parse_group_list("CN=Group,DC=example,DC=com;")
        assert result == ["CN=Group,DC=example,DC=com"]


class TestIsMemberOfGroups:
    def test_exact_full_dn_match(self):
        user_groups = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        required = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        assert _is_member_of_groups(user_groups, required) is True

    def test_case_insensitive_match(self):
        user_groups = ["cn=whisper_users,cn=users,dc=midow,dc=local"]
        required = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        assert _is_member_of_groups(user_groups, required) is True

    def test_no_match(self):
        user_groups = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        required = ["CN=OtherGroup,DC=midow,DC=local"]
        assert _is_member_of_groups(user_groups, required) is False

    def test_match_any_of_multiple_required(self):
        user_groups = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        required = [
            "CN=OtherGroup,DC=midow,DC=local",
            "CN=Whisper_Users,CN=Users,DC=midow,DC=local",
        ]
        assert _is_member_of_groups(user_groups, required) is True

    def test_empty_required_groups_allows_all(self):
        assert _is_member_of_groups(["CN=AnyGroup,DC=example,DC=com"], []) is True

    def test_empty_user_groups_denied(self):
        assert _is_member_of_groups([], ["CN=Required,DC=example,DC=com"]) is False

    def test_whitespace_stripped(self):
        user_groups = ["  CN=Whisper_Users,CN=Users,DC=midow,DC=local  "]
        required = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        assert _is_member_of_groups(user_groups, required) is True

    def test_partial_dn_does_not_match(self):
        """Partial DN config should NOT match — use the full DN from your LDAP logs."""
        user_groups = ["CN=Whisper_Users,CN=Users,DC=midow,DC=local"]
        required = ["CN=Whisper_Users"]
        assert _is_member_of_groups(user_groups, required) is False


class TestEscapeLdapFilter:
    def test_escapes_backslash(self):
        assert _escape_ldap_filter("a\\b") == "a\\5cb"

    def test_escapes_wildcard(self):
        assert _escape_ldap_filter("a*b") == "a\\2ab"

    def test_escapes_parens(self):
        assert _escape_ldap_filter("(uid=x)") == "\\28uid=x\\29"

    def test_escapes_null_byte(self):
        assert _escape_ldap_filter("a\x00b") == "a\\00b"

    def test_safe_string_unchanged(self):
        assert _escape_ldap_filter("john.doe") == "john.doe"

    def test_injection_attempt(self):
        """Classic LDAP injection that would bypass auth without escaping."""
        malicious = "admin)(|(password=*)"
        escaped = _escape_ldap_filter(malicious)
        assert "(" not in escaped
        assert ")" not in escaped
        assert "*" not in escaped


class TestIsValidEmail:
    def test_valid_email(self):
        assert _is_valid_email("user@example.com") is True

    def test_valid_email_subdomain(self):
        assert _is_valid_email("user@mail.example.co.uk") is True

    def test_empty_string(self):
        assert _is_valid_email("") is False

    def test_missing_at(self):
        assert _is_valid_email("userexample.com") is False

    def test_missing_domain(self):
        assert _is_valid_email("user@") is False

    def test_missing_tld(self):
        assert _is_valid_email("user@example") is False

    def test_spaces_rejected(self):
        assert _is_valid_email("user @example.com") is False


class TestIsLdapAdmin:
    def _cfg(self, admin_users="", admin_groups="", recursive_groups=False) -> LdapConfig:
        return LdapConfig(
            enabled=True,
            server="ldap.example.com",
            search_base="DC=example,DC=com",
            admin_users=admin_users,
            admin_groups=admin_groups,
            recursive_groups=recursive_groups,
        )

    def test_admin_by_username(self):
        cfg = self._cfg(admin_users="alice,bob")
        assert _is_ldap_admin(cfg, "alice", []) is True

    def test_admin_username_case_insensitive(self):
        cfg = self._cfg(admin_users="Alice")
        assert _is_ldap_admin(cfg, "alice", []) is True

    def test_non_admin_username(self):
        cfg = self._cfg(admin_users="alice")
        assert _is_ldap_admin(cfg, "bob", []) is False

    def test_admin_by_group_full_dn(self):
        cfg = self._cfg(admin_groups="CN=Admins,CN=Users,DC=example,DC=com")
        user_groups = ["CN=Admins,CN=Users,DC=example,DC=com"]
        assert _is_ldap_admin(cfg, "bob", user_groups) is True

    def test_non_admin_wrong_group(self):
        cfg = self._cfg(admin_groups="CN=Admins,CN=Users,DC=example,DC=com")
        user_groups = ["CN=Whisper_Users,CN=Users,DC=example,DC=com"]
        assert _is_ldap_admin(cfg, "bob", user_groups) is False

    def test_no_admin_config_returns_false(self):
        cfg = self._cfg()
        assert _is_ldap_admin(cfg, "alice", ["CN=Admins,DC=example,DC=com"]) is False

    def test_multiple_admin_groups_semicolon_delimited(self):
        cfg = self._cfg(
            admin_groups=("CN=SuperAdmins,DC=example,DC=com;CN=Admins,CN=Users,DC=example,DC=com")
        )
        user_groups = ["CN=Admins,CN=Users,DC=example,DC=com"]
        assert _is_ldap_admin(cfg, "bob", user_groups) is True
