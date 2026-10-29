"""``app.auth.constants`` keeps exporting the names the cloud edition imports from it."""

import pytest

from app.auth import constants
from app.core import auth_types

pytestmark = pytest.mark.unit

SEAM_NAMES = (
    "AUTH_TYPE_LDAP",
    "AUTH_TYPE_LOCAL",
    "AUTH_TYPE_OIDC",
    "AUTH_TYPE_PKI",
    "AUTH_TYPE_PROXY",
    "AUTH_TYPE_SAML",
    "DB_ADMITTED_AUTH_TYPES",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_MFA",
    "TOKEN_TYPE_REFRESH",
    "VALID_AUTH_TYPES",
    "auth_type_check_sql",
)


@pytest.mark.parametrize("name", SEAM_NAMES)
def test_moved_names_are_still_exported_from_the_seam_module(name: str) -> None:
    assert name in constants.__all__
    assert getattr(constants, name) is getattr(auth_types, name)


@pytest.mark.parametrize("name", ["CLOUD_SEAM_VERSION", "EXTERNAL_AUTH_NO_PASSWORD"])
def test_names_defined_in_the_seam_module_are_exported(name: str) -> None:
    assert name in constants.__all__
    assert getattr(constants, name)
