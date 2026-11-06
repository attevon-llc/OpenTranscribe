"""The LDAP operator documentation and the ``LDAP_*`` setting comments say what the code does.

Both were written once and drifted. ``docs/LDAP_AUTH.md`` documents error strings that do
not exist, reads the local-login fallback log line as a database fault, a SQL recipe that
plants an empty password hash, and omits the group settings entirely; ``config.py`` calls the two group
lists comma-separated when the parser splits them on semicolons (a DN contains commas).

Every test here was written for the CORRECT state and fails on the tree it was written
against (P3 defects D-15 and D-2). They are static on purpose: the claims are prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
DOC = _REPO / "docs" / "LDAP_AUTH.md"
CONFIG = _REPO / "backend" / "app" / "core" / "config.py"

#: Every LDAP setting an operator can put in ``.env`` (P3 spec section 3.2).
SETTINGS = (
    "LDAP_ENABLED",
    "LDAP_SERVER",
    "LDAP_PORT",
    "LDAP_USE_SSL",
    "LDAP_USE_TLS",
    "LDAP_CA_CERT_FILE",
    "LDAP_TLS_SKIP_VERIFY",
    "LDAP_BIND_DN",
    "LDAP_BIND_PASSWORD",
    "LDAP_SEARCH_BASE",
    "LDAP_USERNAME_ATTR",
    "LDAP_USER_SEARCH_FILTER",
    "LDAP_EMAIL_ATTR",
    "LDAP_NAME_ATTR",
    "LDAP_TIMEOUT",
    "LDAP_ADMIN_USERS",
    "LDAP_ADMIN_GROUPS",
    "LDAP_USER_GROUPS",
    "LDAP_RECURSIVE_GROUPS",
    "LDAP_GROUP_ATTR",
)

#: Claims that are false today (P3 spec section 8, "Do not carry over"), as patterns.
FALSE_CLAIMS = {
    "that the local-login fallback log line means a database fault": r"direct\s+(DB|database)\s+"
    r"connection\s+(issue|problem)",
    "an error string that does not exist": r"Password change not allowed for LDAP users",
    "another error string that does not exist": r"cannot use password auth",
    "a third error string that does not exist": r"User has no email attribute",
    "an SQL recipe that plants an empty password hash": r"hashed_password\s*=\s*''",
    "the pre-widening auth_type column as the current schema": r"auth_type\s+VARCHAR\(10\)",
    "that SSL plus StartTLS prefers StartTLS": r"prefers?\s+StartTLS",
    "hand-written compose commands instead of ./opentr.sh": r"docker[ -]compose\s+-f",
    "that LDAP_RECURSIVE_GROUPS reads booleans differently from the other settings": r"LDAP_RECURSIVE_GROUPS`?:\s+only\s+`?true",
    "that every LDAP setting is stored encrypted": r"(all|every)\s+LDAP\s+settings?\s+(are|is)\s+"
    r"(stored\s+)?encrypted",
}

#: Facts the doc must state (P3 spec section 8, items 2-14), as literal substrings.
REQUIRED = {
    "the semicolon delimiter for group lists": "semicolon",
    "the dev overlay command": "./opentr.sh start dev --with-ldap-test",
    "the overlay's network alias": "lldap-test",
    "the admin link-identity action": "link-identity",
    "the read-timeout log message": "did not answer within",
    "the group-mapping page": "authentication/groups",
}


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SETTINGS)
def test_the_doc_names_every_setting(doc, name):
    assert re.search(rf"\b{name}\b", doc), f"{DOC.name} never mentions {name}"


@pytest.mark.parametrize("what", sorted(FALSE_CLAIMS))
def test_the_doc_makes_no_false_claim(doc, what):
    match = re.search(FALSE_CLAIMS[what], doc, re.IGNORECASE)
    assert match is None, f"{DOC.name} claims {what}: {match.group(0)!r}"


@pytest.mark.parametrize("what", sorted(REQUIRED))
def test_the_doc_states_the_fact(doc, what):
    assert REQUIRED[what] in doc, f"{DOC.name} does not state {what}"


def _comment_for(lines: list[str], setting: str) -> str:
    """The contiguous ``#`` block directly above a declaration plus its inline comment."""
    (index,) = [i for i, line in enumerate(lines) if re.match(rf"\s*{setting}\s*:", line)]
    parts = [lines[index].partition("#")[2]]
    above = index - 1
    while above >= 0 and lines[above].strip().startswith("#"):
        parts.append(lines[above].strip().lstrip("#"))
        above -= 1
    return " ".join(parts)


@pytest.mark.parametrize("setting", ["LDAP_ADMIN_GROUPS", "LDAP_USER_GROUPS"])
def test_the_group_list_comments_do_not_say_comma_separated(setting):
    """P3 defect D-2: both lists are split on ``;``; a comma is part of every DN."""
    comment = _comment_for(CONFIG.read_text(encoding="utf-8").splitlines(), setting)

    assert not re.search(r"comma[- ]separated", comment, re.IGNORECASE), comment.strip()
    if comment.strip():
        assert "semicolon" in comment.lower(), f"{setting}'s comment omits the delimiter"
